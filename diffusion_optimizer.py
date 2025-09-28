from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import torch
import torch.nn as nn
from safetensors.torch import load_file

TensorDict = Dict[str, torch.Tensor]


@dataclass(frozen=True)
class TensorInfo:
    key: str
    shape: torch.Size
    dtype: torch.dtype
    numel: int


def flatten_state_dict(state_dict: TensorDict) -> Tuple[torch.Tensor, List[TensorInfo]]:
    if not state_dict:
        raise ValueError("state_dict must not be empty")

    flat_tensors: List[torch.Tensor] = []
    metadata: List[TensorInfo] = []

    for key, tensor in state_dict.items():
        if not torch.is_floating_point(tensor):
            raise TypeError(f"Tensor '{key}' must be floating point")

        flattened = tensor.detach().reshape(-1).to(torch.float32)
        flat_tensors.append(flattened)
        metadata.append(TensorInfo(key=key, shape=tensor.shape, dtype=tensor.dtype, numel=tensor.numel()))

    return torch.cat(flat_tensors, dim=0), metadata


def unflatten_to_state_dict(flat_vector: torch.Tensor, metadata: Sequence[TensorInfo]) -> TensorDict:
    if flat_vector.ndim != 1:
        raise ValueError("flat_vector must be one-dimensional")

    total = sum(info.numel for info in metadata)
    if flat_vector.numel() != total:
        raise ValueError("flat_vector length does not match metadata")

    state_dict: TensorDict = {}
    offset = 0
    for info in metadata:
        chunk = flat_vector[offset : offset + info.numel]
        state_dict[info.key] = chunk.reshape(info.shape).to(info.dtype)
        offset += info.numel

    return state_dict


class ModelZooCorpus(torch.utils.data.Dataset):
    def __init__(self, weight_files: Sequence[Path | str]):
        if not weight_files:
            raise ValueError("weight_files must not be empty")

        self._paths = [Path(path) for path in weight_files]
        self._vectors: List[torch.Tensor] = []
        self.metadata: List[List[TensorInfo]] = []

        for path in self._paths:
            state_dict = load_file(str(path))
            flat, info = flatten_state_dict(state_dict)
            self._vectors.append(flat)
            self.metadata.append(info)

        lengths = {vec.numel() for vec in self._vectors}
        if len(lengths) != 1:
            raise ValueError("All weight vectors must share the same dimensionality")

        self.vector_dim = lengths.pop()
        stacked = torch.stack(self._vectors, dim=0)
        self.mean = stacked.mean(dim=0)
        self.std = stacked.std(dim=0, unbiased=False)
        self.std = torch.where(self.std < 1e-6, torch.ones_like(self.std), self.std)
        self._vectors = stacked

    def __len__(self) -> int:
        return self._vectors.shape[0]

    def __getitem__(self, index: int) -> Tuple[torch.Tensor, List[TensorInfo]]:
        normalized = (self._vectors[index] - self.mean) / self.std
        return normalized.clone(), self.metadata[index]

    @property
    def vectors(self) -> torch.Tensor:
        return self._vectors

    def denormalize(self, normalized_vector: torch.Tensor) -> torch.Tensor:
        if normalized_vector.shape[-1] != self.vector_dim:
            raise ValueError("normalized_vector has incorrect dimensionality")

        mean = self.mean.to(normalized_vector.device)
        std = self.std.to(normalized_vector.device)
        return normalized_vector * std + mean


class CosineSchedule:
    def __init__(self, timesteps: int, s: float = 0.008):
        if timesteps <= 0:
            raise ValueError("timesteps must be positive")

        steps = torch.arange(timesteps + 1, dtype=torch.float64)
        alphas_cumprod = torch.cos(((steps / timesteps) + s) / (1 + s) * math.pi / 2) ** 2
        alphas_cumprod = alphas_cumprod / alphas_cumprod[0]

        betas = 1 - (alphas_cumprod[1:] / alphas_cumprod[:-1])
        betas = betas.clamp(1e-7, 0.999)

        self.betas = betas.to(dtype=torch.float32)
        self.alphas = (1.0 - self.betas).to(dtype=torch.float32)
        self.alpha_bar = torch.cumprod(self.alphas, dim=0)


class ResidualBlock(nn.Module):
    def __init__(self, dim: int, dropout: float = 0.0):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.linear1 = nn.Linear(dim, dim)
        self.activation = nn.SiLU()
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim, dim)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        hidden = self.norm(inputs)
        hidden = self.activation(self.linear1(hidden))
        hidden = self.dropout(hidden)
        hidden = self.linear2(hidden)
        return inputs + hidden


class MLPWeightSynthesizer(nn.Module):
    def __init__(self, vector_dim: int, timesteps: int, dropout: float = 0.1, num_residual_layers: int = 3):
        super().__init__()
        self.vector_dim = vector_dim
        self.timesteps = timesteps
        self.input_layer = nn.Linear(vector_dim + 1, 512)
        self.activation = nn.SiLU()
        self.residual_layers = nn.ModuleList(
            ResidualBlock(512, dropout=dropout) for _ in range(num_residual_layers)
        )
        self.final_norm = nn.LayerNorm(512)
        self.output_layer = nn.Linear(512, vector_dim)

    def forward(self, inputs: torch.Tensor, timesteps: torch.Tensor) -> torch.Tensor:
        if inputs.ndim != 2 or inputs.shape[1] != self.vector_dim:
            raise ValueError("inputs must be of shape (batch, vector_dim)")

        if timesteps.ndim != 1 or timesteps.shape[0] != inputs.shape[0]:
            raise ValueError("timesteps must be one-dimensional with length batch size")

        time_feature = timesteps.to(inputs.dtype).unsqueeze(1)
        if self.timesteps > 1:
            time_feature = time_feature / float(self.timesteps - 1)

        features = torch.cat([inputs, time_feature], dim=1)
        hidden = self.activation(self.input_layer(features))
        for layer in self.residual_layers:
            hidden = layer(hidden)
        hidden = self.activation(self.final_norm(hidden))
        return self.output_layer(hidden)


class DiffusionSynthesizer(nn.Module):
    def __init__(self, vector_dim: int, timesteps: int = 1000, dropout: float = 0.1, residual_layers: int = 3):
        super().__init__()
        self.vector_dim = vector_dim
        self.timesteps = timesteps
        self.network = MLPWeightSynthesizer(vector_dim, timesteps, dropout=dropout, num_residual_layers=residual_layers)

        schedule = CosineSchedule(timesteps)
        self.register_buffer("betas", schedule.betas)
        self.register_buffer("alphas", schedule.alphas)
        self.register_buffer("alpha_bar", schedule.alpha_bar)
        self.register_buffer("sqrt_recip_alpha", torch.sqrt(1.0 / schedule.alphas))
        self.register_buffer("sqrt_one_minus_alpha_bar", torch.sqrt(1.0 - schedule.alpha_bar))
        self.register_buffer("sqrt_betas", torch.sqrt(schedule.betas))

    def forward(self, inputs: torch.Tensor, timesteps: torch.Tensor) -> torch.Tensor:
        return self.network(inputs, timesteps)

    def _extract(self, buffer: torch.Tensor, timesteps: torch.Tensor) -> torch.Tensor:
        values = buffer.index_select(0, timesteps.detach().cpu()).to(timesteps.device)
        return values.unsqueeze(-1)

    def _random_normal(
        self, shape: Tuple[int, ...], device: torch.device, dtype: torch.dtype, generator: torch.Generator | None
    ) -> torch.Tensor:
        if generator is None:
            return torch.randn(shape, device=device, dtype=dtype)
        return torch.randn(shape, device=device, dtype=dtype, generator=generator)

    def predict(self, noisy_weights: torch.Tensor, timesteps: torch.Tensor) -> torch.Tensor:
        return self.forward(noisy_weights, timesteps)

    def compute_loss(self, clean_weights: torch.Tensor) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        if clean_weights.ndim != 2 or clean_weights.shape[1] != self.vector_dim:
            raise ValueError("clean_weights must be of shape (batch, vector_dim)")

        batch_size = clean_weights.shape[0]
        device = clean_weights.device
        timesteps = torch.randint(0, self.timesteps, (batch_size,), device=device)
        noise = torch.randn_like(clean_weights)

        sqrt_alpha_bar_t = torch.sqrt(self._extract(self.alpha_bar, timesteps))
        sqrt_one_minus_alpha_bar_t = torch.clamp(
            self._extract(self.sqrt_one_minus_alpha_bar, timesteps), min=1e-7
        )
        noisy_weights = sqrt_alpha_bar_t * clean_weights + sqrt_one_minus_alpha_bar_t * noise
        predicted_noise = self.predict(noisy_weights, timesteps)
        loss = torch.mean((noise - predicted_noise) ** 2)

        return loss, {
            "timesteps": timesteps,
            "noise": noise,
            "noisy_weights": noisy_weights,
            "predicted_noise": predicted_noise,
        }

    @torch.no_grad()
    def sample(self, num_samples: int, device: torch.device | None = None, generator: torch.Generator | None = None) -> torch.Tensor:
        if num_samples <= 0:
            raise ValueError("num_samples must be positive")

        if device is None:
            device = self.betas.device

        samples = self._random_normal((num_samples, self.vector_dim), device, torch.float32, generator)

        for step in reversed(range(self.timesteps)):
            timesteps = torch.full((num_samples,), step, device=device, dtype=torch.long)
            predicted_noise = self.predict(samples, timesteps)

            sqrt_recip_alpha_t = self._extract(self.sqrt_recip_alpha, timesteps)
            sqrt_one_minus_alpha_bar_t = torch.clamp(
                self._extract(self.sqrt_one_minus_alpha_bar, timesteps), min=1e-7
            )
            beta_t = self._extract(self.betas, timesteps)

            samples = sqrt_recip_alpha_t * (samples - (beta_t / sqrt_one_minus_alpha_bar_t) * predicted_noise)

            if step > 0:
                noise = self._random_normal(samples.shape, device, samples.dtype, generator)
                sigma_t = self._extract(self.sqrt_betas, timesteps)
                samples = samples + sigma_t * noise

        return samples
