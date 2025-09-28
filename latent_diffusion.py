import math
from typing import Tuple

import torch
import torch.nn as nn

from perceiver_ae import PerceiverAE


class TimestepEmbed(nn.Module):
    def __init__(self, dim: int) -> None:
        super().__init__()
        self.fc1 = nn.Linear(1, dim)
        self.act = nn.SiLU()
        self.fc2 = nn.Linear(dim, dim)

    def forward(self, t_scalar: torch.Tensor) -> torch.Tensor:
        # t_scalar: [B, 1] in [0, 1]
        x = self.fc1(t_scalar)
        x = self.act(x)
        x = self.fc2(x)
        return x


class LatentDenoiser(nn.Module):
    """Small MLP denoiser on z. Input: noisy z_t and t → predict ε."""

    def __init__(self, z_dim: int = 512, hidden: int = 1024) -> None:
        super().__init__()
        self.tproj = TimestepEmbed(z_dim)
        self.net = nn.Sequential(
            nn.Linear(z_dim, hidden),
            nn.SiLU(),
            nn.Linear(hidden, hidden),
            nn.SiLU(),
            nn.Linear(hidden, z_dim),
        )

    def forward(self, z_noisy: torch.Tensor, t01: torch.Tensor) -> torch.Tensor:
        h = z_noisy + self.tproj(t01)
        return self.net(h)


class CosineSchedule:
    """Classic cosine beta schedule utilities."""

    def __init__(self, T: int = 1000) -> None:
        if T <= 0:
            raise ValueError("Number of diffusion steps T must be positive")
        self.T = T
        s = 0.008
        steps = torch.arange(T + 1, dtype=torch.float32)
        alphas_cumprod = torch.cos(((steps / T) + s) / (1 + s) * math.pi * 0.5) ** 2
        alphas_cumprod = alphas_cumprod / alphas_cumprod[0]
        self.alphas_bar = alphas_cumprod  # [0..T]

    def to(self, device: torch.device | str) -> "CosineSchedule":
        self.alphas_bar = self.alphas_bar.to(device)
        return self


def q_sample(
    z0: torch.Tensor, t: torch.Tensor, sched: CosineSchedule, noise: torch.Tensor | None = None
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Forward diffusion: z_t = sqrt(alpha_bar_t) z0 + sqrt(1 - alpha_bar_t) * ε."""

    if noise is None:
        noise = torch.randn_like(z0)
    a = sched.alphas_bar[t].view(-1, 1)
    return (a.sqrt() * z0) + ((1 - a).sqrt() * noise), noise


@torch.no_grad()
def ddim_sample(
    denoiser: nn.Module,
    sched: CosineSchedule,
    z_dim: int = 512,
    steps: int = 50,
    device: torch.device | str = "cpu",
) -> torch.Tensor:
    if steps < 2:
        raise ValueError("DDIM sampling requires at least two steps")

    device = torch.device(device)
    # Uniform timesteps mapped onto the scheduler range.
    t_cont = torch.linspace(0, sched.T - 1, steps, dtype=torch.float32, device=device)
    t_seq = torch.flip(t_cont.round().long().unique_consecutive(), dims=(0,))
    if t_seq.numel() < 2:
        raise RuntimeError("Time sequence collapsed to fewer than two unique steps")

    z = torch.randn(1, z_dim, device=device)
    for ti, tj in zip(t_seq[:-1], t_seq[1:]):
        t01 = (ti.float() / sched.T).view(1, 1)
        eps = denoiser(z, t01)
        a_t = sched.alphas_bar[ti]
        a_s = sched.alphas_bar[tj]
        # DDIM update (η=0)
        z0 = (z - (1 - a_t).sqrt() * eps) / a_t.sqrt()
        z = a_s.sqrt() * z0 + (1 - a_s).sqrt() * eps
    return z


def train_latent_diffusion(
    ae: PerceiverAE,
    flat_dataset: torch.utils.data.Dataset,
    epochs: int = 5,
    batch_size: int = 16,
    lr: float = 2e-4,
    T: int = 1000,
) -> tuple[LatentDenoiser, CosineSchedule]:
    """Train a diffusion model in the autoencoder's latent space."""

    device = next(ae.parameters()).device
    ae.eval()
    dl = torch.utils.data.DataLoader(flat_dataset, batch_size=batch_size, shuffle=True, drop_last=True)
    sched = CosineSchedule(T=T).to(device)
    denoiser = LatentDenoiser(z_dim=ae.latent_dim).to(device)
    opt = torch.optim.AdamW(denoiser.parameters(), lr=lr)
    mse = nn.MSELoss()

    for epoch in range(epochs):
        total = 0.0
        for x in dl:
            x = x.to(device)
            with torch.no_grad():
                z0 = ae.encode(x)  # [B, z_dim]
            t = torch.randint(low=0, high=T, size=(x.size(0),), device=device)
            zt, eps = q_sample(z0, t, sched)
            t01 = (t.float() / T).view(-1, 1)
            eps_hat = denoiser(zt, t01)
            loss = mse(eps_hat, eps)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item()
        print(f"[LD] epoch {epoch} | eps MSE {total/len(dl):.6f}")

    return denoiser, sched


@torch.no_grad()
def generate_weights(ae: PerceiverAE, denoiser: LatentDenoiser, sched: CosineSchedule) -> torch.Tensor:
    device = next(ae.parameters()).device
    z = ddim_sample(denoiser, sched, z_dim=ae.latent_dim, device=device)
    x_hat = ae.decode(z)
    return x_hat.squeeze(0)
