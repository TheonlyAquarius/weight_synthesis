from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import torch
from safetensors.torch import load_file
from torch import Tensor, nn
from torch.utils.data import DataLoader, Dataset


TensorDict = Dict[str, Tensor]


@dataclass(frozen=True)
class StateMetadata:
    key: str
    shape: torch.Size
    dtype: torch.dtype


def flatten_state_dict(state_dict: TensorDict) -> Tuple[Tensor, List[StateMetadata]]:
    """Flatten a state dict into a single vector preserving reconstruction metadata."""
    flat_tensors: List[Tensor] = []
    metadata: List[StateMetadata] = []

    for key, tensor in state_dict.items():
        data = tensor.detach().float().reshape(-1)
        flat_tensors.append(data)
        metadata.append(StateMetadata(key=key, shape=tensor.shape, dtype=tensor.dtype))

    flat_vector = torch.cat(flat_tensors)
    return flat_vector, metadata


def unflatten_to_state_dict(
    flat_vector: Tensor, metadata: Sequence[StateMetadata]
) -> OrderedDict:
    """Reconstruct a state dict from a flattened vector and associated metadata."""
    state_dict: OrderedDict[str, Tensor] = OrderedDict()
    cursor = 0

    for entry in metadata:
        numel = math.prod(entry.shape)
        segment = flat_vector[cursor : cursor + numel]
        cursor += numel
        tensor = segment.reshape(entry.shape).to(entry.dtype)
        state_dict[entry.key] = tensor

    if cursor != flat_vector.numel():
        raise ValueError("Flat vector length does not match provided metadata.")

    return state_dict


class ModelZooDataset(Dataset):
    """Corpus of flattened weight vectors sourced from .safetensors checkpoints."""

    def __init__(self, paths: Iterable[Path | str]):
        resolved_paths = [Path(p) for p in paths]
        if not resolved_paths:
            raise ValueError("Model zoo dataset requires at least one checkpoint path.")

        vectors: List[Tensor] = []
        self._metadata: List[List[StateMetadata]] = []
        self._paths: List[Path] = []

        reference_dim: int | None = None
        for path in resolved_paths:
            weights = load_file(str(path))
            flat, metadata = flatten_state_dict(weights)
            if reference_dim is None:
                reference_dim = flat.numel()
            if flat.numel() != reference_dim:
                raise ValueError(
                    "All checkpoints must share the same flattened dimensionality."
                )
            vectors.append(flat)
            self._metadata.append(metadata)
            self._paths.append(path)

        stacked = torch.stack(vectors, dim=0)
        self._mean = stacked.mean(dim=0)
        self._std = stacked.std(dim=0, unbiased=False).clamp_min(1e-6)
        self._normalized = (stacked - self._mean) / self._std

    @property
    def feature_dim(self) -> int:
        return self._normalized.shape[1]

    @property
    def stats(self) -> Tuple[Tensor, Tensor]:
        return self._mean.clone(), self._std.clone()

    def metadata_for_index(self, index: int) -> Sequence[StateMetadata]:
        return self._metadata[index]

    def checkpoint_path(self, index: int) -> Path:
        return self._paths[index]

    def denormalize(self, normalized_vector: Tensor) -> Tensor:
        return normalized_vector * self._std + self._mean

    def __len__(self) -> int:  # type: ignore[override]
        return self._normalized.shape[0]

    def __getitem__(self, index: int) -> Tensor:  # type: ignore[override]
        return self._normalized[index]


class DiffusionSchedule:
    """Cosine diffusion schedule storing precomputed diffusion coefficients."""

    def __init__(self, timesteps: int) -> None:
        if timesteps <= 0:
            raise ValueError("timesteps must be positive")
        self.timesteps = timesteps
        self._betas = self._cosine_beta_schedule(timesteps)
        self._alphas = 1.0 - self._betas
        alphabar = torch.cumprod(self._alphas, dim=0)
        self._alphabar = torch.cat([torch.ones(1), alphabar])
        self._sqrt_alphabar = torch.sqrt(self._alphabar)
        self._sqrt_one_minus_alphabar = torch.sqrt(1.0 - self._alphabar)
        self._sqrt_recip_alphas = torch.sqrt(1.0 / self._alphas)
        alphabar_prev = self._alphabar[:-1]
        alphabar_t = self._alphabar[1:]
        posterior_variance = self._betas * (1.0 - alphabar_prev) / (1.0 - alphabar_t)
        self._posterior_variance = posterior_variance.clamp(min=1e-20)

    @staticmethod
    def _cosine_beta_schedule(timesteps: int) -> Tensor:
        steps = timesteps + 1
        x = torch.linspace(0, timesteps, steps)
        alphas_cumprod = torch.cos(((x / timesteps) + 0.008) / 1.008 * math.pi / 2) ** 2
        alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
        betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
        return betas.clamp(0.0001, 0.9999)

    @property
    def betas(self) -> Tensor:
        return self._betas.clone()

    @property
    def sqrt_alphabar(self) -> Tensor:
        return self._sqrt_alphabar.clone()

    @property
    def sqrt_one_minus_alphabar(self) -> Tensor:
        return self._sqrt_one_minus_alphabar.clone()

    @property
    def sqrt_recip_alphas(self) -> Tensor:
        return self._sqrt_recip_alphas.clone()

    @property
    def posterior_variance(self) -> Tensor:
        return self._posterior_variance.clone()


class WeightDiffusionMLP(nn.Module):
    """Architecture-agnostic noise predictor operating on flattened weight vectors."""

    def __init__(self, parameter_dim: int, bottleneck_dim: int = 512) -> None:
        super().__init__()
        self.parameter_dim = parameter_dim
        self.fc1 = nn.Linear(parameter_dim + 1, bottleneck_dim)
        self.fc2 = nn.Linear(bottleneck_dim, bottleneck_dim)
        self.fc3 = nn.Linear(bottleneck_dim, parameter_dim)
        self.activation = nn.SiLU()

    def forward(self, x: Tensor, timesteps: Tensor) -> Tensor:  # type: ignore[override]
        if timesteps.ndim == 1:
            t = timesteps.unsqueeze(1)
        elif timesteps.ndim == 2 and timesteps.shape[1] == 1:
            t = timesteps
        else:
            raise ValueError("timesteps tensor must be of shape (batch,) or (batch, 1)")
        if x.shape[0] != t.shape[0]:
            raise ValueError("Mismatch between batch size of inputs and timesteps")
        concatenated = torch.cat([x, t], dim=1)
        h = self.activation(self.fc1(concatenated))
        h = self.activation(self.fc2(h))
        return self.fc3(h)


def _prepare_dataloader(dataset: ModelZooDataset, batch_size: int) -> DataLoader:
    drop_last = len(dataset) >= batch_size and batch_size > 0
    return DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=drop_last)


def train_synthesizer(
    model: WeightDiffusionMLP,
    dataset: ModelZooDataset,
    schedule: DiffusionSchedule,
    epochs: int,
    batch_size: int,
    lr: float,
    device: torch.device | str = "cpu",
) -> List[float]:
    if epochs <= 0:
        raise ValueError("epochs must be positive")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if lr <= 0:
        raise ValueError("learning rate must be positive")

    device = torch.device(device)
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
    losses: List[float] = []
    dataloader = _prepare_dataloader(dataset, batch_size)

    for _ in range(epochs):
        for batch in dataloader:
            x0 = batch.to(device)
            batch_size = x0.shape[0]
            t = torch.randint(0, schedule.timesteps + 1, (batch_size,), device=device)
            sqrt_alphabar = schedule.sqrt_alphabar.to(device)[t]
            sqrt_one_minus = schedule.sqrt_one_minus_alphabar.to(device)[t]
            noise = torch.randn_like(x0)
            xt = sqrt_alphabar.unsqueeze(1) * x0 + sqrt_one_minus.unsqueeze(1) * noise
            t_normalized = t.float() / schedule.timesteps
            predicted_noise = model(xt, t_normalized)
            loss = nn.functional.mse_loss(predicted_noise, noise)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())

    return losses


def generate_weight_vectors(
    model: WeightDiffusionMLP,
    schedule: DiffusionSchedule,
    mean: Tensor,
    std: Tensor,
    metadata: Sequence[StateMetadata],
    num_samples: int,
    device: torch.device | str = "cpu",
) -> List[OrderedDict]:
    if num_samples <= 0:
        raise ValueError("num_samples must be positive")

    device = torch.device(device)
    model = model.to(device)
    model.eval()
    parameter_dim = mean.numel()
    mean = mean.to(device)
    std = std.to(device)

    samples: List[OrderedDict] = []
    with torch.no_grad():
        xt = torch.randn(num_samples, parameter_dim, device=device)
        for timestep in range(schedule.timesteps, 0, -1):
            t_tensor = torch.full((num_samples,), timestep, device=device)
            t_normalized = t_tensor.float() / schedule.timesteps
            predicted_noise = model(xt, t_normalized)
            beta_t = schedule.betas.to(device)[timestep - 1]
            sqrt_recip_alpha = schedule.sqrt_recip_alphas.to(device)[timestep - 1]
            sqrt_one_minus = schedule.sqrt_one_minus_alphabar.to(device)[timestep]
            model_mean = (
                sqrt_recip_alpha * (xt - beta_t / sqrt_one_minus * predicted_noise)
            )
            if timestep > 1:
                noise = torch.randn_like(xt)
                variance = schedule.posterior_variance.to(device)[timestep - 1]
                xt = model_mean + torch.sqrt(variance) * noise
            else:
                xt = model_mean

        x0 = xt * std.unsqueeze(0) + mean.unsqueeze(0)
        for vector in x0:
            samples.append(unflatten_to_state_dict(vector.cpu(), metadata))

    return samples


def compute_weight_statistics(state_dict: TensorDict) -> Dict[str, float]:
    flat, _ = flatten_state_dict(state_dict)
    mean = flat.mean().item()
    variance = flat.var(unbiased=False).item()
    std = math.sqrt(variance)
    centered = (flat - flat.mean()) / (std + 1e-8)
    kurtosis = torch.mean(centered ** 4).item()
    return {"mean": mean, "variance": variance, "std": std, "kurtosis": kurtosis}


def compute_spectral_properties(state_dict: TensorDict) -> Dict[str, Tensor]:
    spectra: Dict[str, Tensor] = {}
    for key, tensor in state_dict.items():
        if tensor.ndim < 2:
            continue
        matrix = tensor.reshape(tensor.shape[0], -1).float()
        cov = matrix @ matrix.transpose(0, 1) / matrix.shape[1]
        eigenvalues = torch.linalg.eigvalsh(cov.cpu())
        spectra[key] = eigenvalues
    return spectra


__all__ = [
    "ModelZooDataset",
    "DiffusionSchedule",
    "WeightDiffusionMLP",
    "flatten_state_dict",
    "unflatten_to_state_dict",
    "train_synthesizer",
    "generate_weight_vectors",
    "compute_weight_statistics",
    "compute_spectral_properties",
]
