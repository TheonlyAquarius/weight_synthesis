import glob
import os
from typing import List

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
from safetensors.torch import load_file

from perceiver_ae import PerceiverAE


def flatten_state_dict(state_dict: dict[str, torch.Tensor]) -> torch.Tensor:
    """Flatten a state dict into a single vector with a deterministic key order."""

    flat_parts: List[torch.Tensor] = []
    for key in sorted(state_dict.keys()):
        flat_parts.append(state_dict[key].reshape(-1))
    return torch.cat(flat_parts, dim=0)


class FlatWeightsDataset(Dataset):
    def __init__(self, dir_with_safetensors: str) -> None:
        paths = sorted(glob.glob(os.path.join(dir_with_safetensors, "*.safetensors")))
        if not paths:
            raise FileNotFoundError(f"No .safetensors in {dir_with_safetensors}")
        self.paths = paths

        sample = load_file(self.paths[0])
        self.D = sum(param.numel() for param in sample.values())

    def __len__(self) -> int:
        return len(self.paths)

    def __getitem__(self, idx: int) -> torch.Tensor:
        sd = load_file(self.paths[idx])
        x = flatten_state_dict(sd).float()
        return x


def train_autoencoder(
    data_dir: str,
    epochs: int = 5,
    batch_size: int = 2,
    lr: float = 1e-4,
    num_latents: int = 64,
    latent_dim: int = 512,
    depth: int = 6,
    device: torch.device | None = None,
) -> PerceiverAE:
    dataset = FlatWeightsDataset(data_dir)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True)

    ae = PerceiverAE(D=dataset.D, num_latents=num_latents, latent_dim=latent_dim, depth=depth)
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ae = ae.to(device)

    opt = torch.optim.AdamW(ae.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    for epoch in range(epochs):
        ae.train()
        total = 0.0
        for batch in dataloader:
            batch = batch.to(device)
            recon, _ = ae(batch)
            loss = loss_fn(recon, batch)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item()
        print(f"[AE] epoch {epoch} | loss {total / len(dataloader):.6f}")

    return ae


__all__ = ["FlatWeightsDataset", "train_autoencoder", "flatten_state_dict"]
