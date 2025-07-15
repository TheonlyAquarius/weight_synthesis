import torch
import torch.nn as nn
import math

class MultiHeadSelfAttention(nn.Module):
    def __init__(self, embed_dim, num_heads):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.head_dim = embed_dim // num_heads

        assert (
            self.head_dim * self.num_heads == self.embed_dim
        ), "embed_dim must be divisible by num_heads"

        self.values = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.keys = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.queries = nn.Linear(self.head_dim, self.head_dim, bias=False)
        self.fc_out = nn.Linear(self.num_heads * self.head_dim, embed_dim)

    def forward(self, values, keys, query, mask):
        N = query.shape[0]
        value_len, key_len, query_len = values.shape[1], keys.shape[1], query.shape[1]

        # Split the embedding into self.num_heads different pieces
        values = values.reshape(N, value_len, self.num_heads, self.head_dim)
        keys = keys.reshape(N, key_len, self.num_heads, self.head_dim)
        queries = query.reshape(N, query_len, self.num_heads, self.head_dim)

        values = self.values(values)
        keys = self.keys(keys)
        queries = self.queries(queries)

        # Einsum does matrix multiplication for query*keys for each training example
        # with every other training example, don't be confused by einsum
        # it's just how I like doing matrix multiplication
        attention = torch.einsum("nqhd,nkhd->nhqk", [queries, keys])

        if mask is not None:
            attention = attention.masked_fill(mask == 0, float("-1e20"))

        attention = torch.softmax(attention / (self.embed_dim ** (1 / 2)), dim=3)

        out = torch.einsum("nhql,nlhd->nqhd", [attention, values]).reshape(
            N, query_len, self.num_heads * self.head_dim
        )

        out = self.fc_out(out)
        return out


class TransformerBlock(nn.Module):
    def __init__(self, embed_dim, num_heads, ff_hidden_dim, dropout):
        super().__init__()
        self.attention = MultiHeadSelfAttention(embed_dim, num_heads)
        self.norm1 = nn.LayerNorm(embed_dim)
        self.norm2 = nn.LayerNorm(embed_dim)

        self.feed_forward = nn.Sequential(
            nn.Linear(embed_dim, ff_hidden_dim),
            nn.ReLU(),
            nn.Linear(ff_hidden_dim, embed_dim),
        )

        self.dropout = nn.Dropout(dropout)

    def forward(self, value, key, query, mask):
        attention = self.attention(value, key, query, mask)

        # Add skip connection, run through normalization and finally dropout
        x = self.dropout(self.norm1(attention + query))
        forward = self.feed_forward(x)
        out = self.dropout(self.norm2(forward + x))
        return out


class DiffusionOptimizer(nn.Module):
    def __init__(
        self,
        embed_dim,
        num_heads,
        num_layers,
        ff_hidden_dim,
        dropout,
        max_timesteps=1000,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.max_timesteps = max_timesteps

        self.layers = nn.ModuleList(
            [
                TransformerBlock(embed_dim, num_heads, ff_hidden_dim, dropout)
                for _ in range(num_layers)
            ]
        )
        self.dropout = nn.Dropout(dropout)

    def forward(self, weights, t):
        # Process one tensor at a time
        denoised_weights = []
        for weight_tensor in weights:
            # 1. Project to embedding dimension
            original_shape = weight_tensor.shape
            if weight_tensor.dim() == 1:
                weight_tensor = weight_tensor.unsqueeze(0) # Add batch dimension if missing

            # Reshape to (batch_size, seq_len, features)
            # For a single tensor, seq_len is the number of elements
            if weight_tensor.dim() > 2:
                # This is a basic way to handle tensors with more than 2 dimensions.
                # It flattens the tensor and then adds a sequence dimension.
                # A more sophisticated approach might use 1x1 convolutions to project
                # to the embedding dimension while preserving spatial structure.
                # However, to avoid hard-coding, we'll use this general approach.
                num_elements = weight_tensor.numel() // weight_tensor.shape[0]
                x = weight_tensor.reshape(weight_tensor.shape[0], num_elements, -1)
            else:
                x = weight_tensor.unsqueeze(1) # Add sequence dimension

            # Ensure the feature dimension matches embed_dim.
            # This is a critical step and requires a flexible way to project.
            # A linear layer can do this, but its input size must be known.
            # We can create it dynamically or use a more flexible projection.
            # For simplicity, we'll assume the last dimension can be projected.
            # This is a potential area for improvement to be more robust.
            feature_dim = x.shape[-1]
            if not hasattr(self, f'input_projection_{feature_dim}'):
                setattr(self, f'input_projection_{feature_dim}', nn.Linear(feature_dim, self.embed_dim))

            x = getattr(self, f'input_projection_{feature_dim}')(x)

            # 2. Add timestep embedding
            timestep_embedding = self.get_timestep_embedding(t, self.embed_dim).to(x.device)
            x += timestep_embedding.unsqueeze(1) # Add to each element in the sequence

            # 3. Transformer blocks
            for layer in self.layers:
                x = layer(x, x, x, mask=None)

            # 4. Project back to original feature dimension
            if not hasattr(self, f'output_projection_{feature_dim}'):
                setattr(self, f'output_projection_{feature_dim}', nn.Linear(self.embed_dim, feature_dim))

            x = getattr(self, f'output_projection_{feature_dim}')(x)

            # 5. Reshape back to original tensor shape
            if len(original_shape) > 2:
                 denoised_tensor = x.reshape(original_shape)
            elif len(original_shape) == 1:
                denoised_tensor = x.squeeze(1).squeeze(0)
            else: # len(original_shape) == 2
                 denoised_tensor = x.squeeze(1)

            denoised_weights.append(denoised_tensor)

        return denoised_weights

    def get_timestep_embedding(self, t, embed_dim):
        half_dim = embed_dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, dtype=torch.float32) * -emb)
        emb = t.float().unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat([torch.sin(emb), torch.cos(emb)], dim=1)
        if embed_dim % 2 == 1:
            emb = torch.nn.functional.pad(emb, (0, 1))
        return emb


if __name__ == "__main__":
    # 1. Define a toy model
    class ToyModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(3, 16, kernel_size=3, stride=1, padding=1)
            self.relu = nn.ReLU()
            self.fc1 = nn.Linear(16 * 28 * 28, 10)

        def forward(self, x):
            x = self.conv1(x)
            x = self.relu(x)
            x = x.view(x.size(0), -1)
            x = self.fc1(x)
            return x

    toy_model = ToyModel()
    weights = list(toy_model.parameters())

    # 2. Initialize the Diffusion Optimizer
    optimizer = DiffusionOptimizer(
        embed_dim=128,
        num_heads=8,
        num_layers=6,
        ff_hidden_dim=256,
        dropout=0.1,
    )

    # 3. Create a dummy timestep
    t = torch.randint(0, 1000, (1,))

    # 4. Get the denoised weights
    denoised_weights = optimizer(weights, t)

    # 5. Check that the output shapes match the input shapes
    for i, (original, denoised) in enumerate(zip(weights, denoised_weights)):
        assert original.shape == denoised.shape, \
            f"Shape mismatch for weight {i}: original {original.shape}, denoised {denoised.shape}"

    print("Diffusion Optimizer ran successfully!")
