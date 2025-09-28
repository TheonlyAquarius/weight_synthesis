import math
from typing import Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F


class PerceiverBlock(nn.Module):
    """Single Perceiver-style block with cross- and self-attention."""

    def __init__(self, latent_dim: int, num_heads: int = 8, ff_mult: int = 4, dropout: float = 0.0):
        super().__init__()
        self.cross_attn = nn.MultiheadAttention(latent_dim, num_heads, batch_first=True)
        self.cross_norm = nn.LayerNorm(latent_dim)
        self.self_attn = nn.MultiheadAttention(latent_dim, num_heads, batch_first=True)
        self.self_norm = nn.LayerNorm(latent_dim)
        self.ff = nn.Sequential(
            nn.LayerNorm(latent_dim),
            nn.Linear(latent_dim, ff_mult * latent_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(ff_mult * latent_dim, latent_dim),
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, latents: torch.Tensor, tokens: torch.Tensor) -> torch.Tensor:
        # Cross-attention: latents query the input tokens.
        cross, _ = self.cross_attn(latents, tokens, tokens, need_weights=False)
        latents = self.cross_norm(latents + self.dropout(cross))

        # Self-attention on the latent array.
        self_out, _ = self.self_attn(latents, latents, latents, need_weights=False)
        latents = self.self_norm(latents + self.dropout(self_out))

        # Feed-forward network.
        latents = latents + self.dropout(self.ff(latents))
        return latents


class PerceiverAE(nn.Module):
    """Autoencoder that compresses a flat parameter vector into a Perceiver latent."""

    def __init__(
        self,
        D: int,
        num_latents: int = 64,
        latent_dim: int = 512,
        depth: int = 6,
        num_heads: int = 8,
        ff_mult: int = 4,
        chunk_size: int | None = None,
        dropout: float = 0.0,
    ) -> None:
        super().__init__()
        if D <= 0:
            raise ValueError("Input dimension D must be positive")

        if chunk_size is None:
            chunk_size = max(1, math.ceil(D / num_latents))
        self.chunk_size = int(chunk_size)
        self.num_chunks = math.ceil(D / self.chunk_size)
        self.padded_dim = self.num_chunks * self.chunk_size

        self.D = int(D)
        self.latent_dim = int(latent_dim)
        self.num_latents = int(num_latents)

        self.input_proj = nn.Linear(self.chunk_size, latent_dim)
        self.latent_array = nn.Parameter(torch.randn(num_latents, latent_dim) * 0.02)
        self.blocks = nn.ModuleList(
            [PerceiverBlock(latent_dim, num_heads=num_heads, ff_mult=ff_mult, dropout=dropout) for _ in range(depth)]
        )
        self.to_latent = nn.Sequential(nn.LayerNorm(latent_dim), nn.Linear(latent_dim, latent_dim))

        self.decoder_latents = nn.Sequential(
            nn.LayerNorm(latent_dim),
            nn.Linear(latent_dim, self.num_chunks * latent_dim),
            nn.GELU(),
        )
        self.chunk_out = nn.Linear(latent_dim, self.chunk_size)

    def _pad(self, x: torch.Tensor) -> torch.Tensor:
        if x.shape[-1] == self.padded_dim:
            return x
        pad = self.padded_dim - x.shape[-1]
        return F.pad(x, (0, pad))

    def _tokenize(self, x: torch.Tensor) -> torch.Tensor:
        x = self._pad(x)
        b = x.shape[0]
        tokens = x.view(b, self.num_chunks, self.chunk_size)
        tokens = self.input_proj(tokens)
        return tokens

    def _latent_template(self, batch_size: int) -> torch.Tensor:
        return self.latent_array.unsqueeze(0).expand(batch_size, -1, -1)

    def encode(self, x: torch.Tensor) -> torch.Tensor:
        tokens = self._tokenize(x)
        latents = self._latent_template(tokens.shape[0])
        for block in self.blocks:
            latents = block(latents, tokens)
        latents = latents.mean(dim=1)
        return self.to_latent(latents)

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        latent_tokens = self.decoder_latents(z)
        latent_tokens = latent_tokens.view(z.shape[0], self.num_chunks, self.latent_dim)
        chunks = self.chunk_out(latent_tokens)
        recon = chunks.view(z.shape[0], -1)[..., : self.D]
        return recon

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        z = self.encode(x)
        recon = self.decode(z)
        return recon, z
