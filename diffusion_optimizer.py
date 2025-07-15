import torch
import torch.nn as nn
import math
from safetensors.torch import load_file, save_file

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
        self.projection_layers = nn.ModuleDict()
        self.unprojection_layers = nn.ModuleDict()

    def _get_projection_key(self, shape):
        return str(list(shape))

    def _ensure_projection_layers(self, key, tensor):
        if key not in self.projection_layers:
            num_features = tensor.numel()
            self.projection_layers[key] = nn.Linear(num_features, self.embed_dim)
            self.unprojection_layers[key] = nn.Linear(self.embed_dim, num_features)

    def forward(self, weights_dict, t):
        device = next(self.parameters()).device
        vectors = []
        shapes = {}
        keys_order = []

        for key, tensor in weights_dict.items():
            shape_key = self._get_projection_key(tensor.shape)
            self._ensure_projection_layers(shape_key, tensor)

            tensor_in = tensor.to(device).flatten()
            vectors.append(self.projection_layers[shape_key](tensor_in))

            shapes[key] = tensor.shape
            keys_order.append(key)

        # Create a sequence tensor
        sequence = torch.stack(vectors, dim=0).unsqueeze(0) # (1, L, embed_dim)

        # Add positional embeddings
        positional_embeddings = self.get_positional_embedding(len(keys_order), self.embed_dim).to(device)
        sequence += positional_embeddings

        # Add timestep embedding
        timestep_embedding = self.get_timestep_embedding(t, self.embed_dim).to(device)
        sequence += timestep_embedding.unsqueeze(1)

        # Transformer blocks
        for layer in self.layers:
            sequence = layer(sequence, sequence, sequence, mask=None)

        # Denoise and reconstruct
        denoised_weights = {}
        for i, key in enumerate(keys_order):
            shape = shapes[key]
            shape_key = self._get_projection_key(shape)
            output_vector = sequence.squeeze(0)[i]

            reconstructed_tensor = self.unprojection_layers[shape_key](output_vector)
            denoised_weights[key] = reconstructed_tensor.reshape(shape)

        return denoised_weights

    def get_positional_embedding(self, seq_len, embed_dim):
        position = torch.arange(seq_len, dtype=torch.float32).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, embed_dim, 2).float() * -(math.log(10000.0) / embed_dim))
        pos_emb = torch.zeros(seq_len, embed_dim)
        pos_emb[:, 0::2] = torch.sin(position * div_term)
        pos_emb[:, 1::2] = torch.cos(position * div_term)
        return pos_emb.unsqueeze(0)

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
    # 1. Define a toy model and save its weights to a .safetensors file
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
    weights = toy_model.state_dict()

    # Save to a temporary file
    temp_weights_file = "temp_weights.safetensors"
    save_file(weights, temp_weights_file)

    # 2. Initialize the Diffusion Optimizer
    optimizer = DiffusionOptimizer(
        embed_dim=128,
        num_heads=8,
        num_layers=6,
        ff_hidden_dim=256,
        dropout=0.1,
    )

    # 3. Load the weights from the .safetensors file
    loaded_weights = load_file(temp_weights_file)

    # 4. Create a dummy timestep
    t = torch.randint(0, 1000, (1,))

    # 5. Get the denoised weights
    denoised_weights = optimizer(loaded_weights, t)

    # 6. Check that the output shapes match the input shapes
    for key in loaded_weights.keys():
        assert loaded_weights[key].shape == denoised_weights[key].shape, \
            f"Shape mismatch for weight {key}: original {loaded_weights[key].shape}, denoised {denoised_weights[key].shape}"

    print("Diffusion Optimizer ran successfully!")

    # 7. Clean up the temporary file
    import os
    os.remove(temp_weights_file)
