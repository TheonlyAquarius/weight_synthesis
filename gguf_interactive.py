import os
import numpy as np
from gguf import GGUFReader, GGUFWriter, GGMLQuantizationType

def view_gguf_info():
    """Views information about a GGUF file."""
    filepath = input("Enter the path to the GGUF file: ")
    if not os.path.exists(filepath):
        print("File not found.")
        return

    try:
        reader = GGUFReader(filepath)
        print("\\n--- GGUF File Information ---")
        print(f"Version: {reader.gguf_version}")
        print(f"Tensor Count: {len(reader.tensors)}")
        print(f"Metadata Keys: {len(reader.fields)}")

        print("\\n--- Metadata ---")
        for key, field in reader.fields.items():
            print(f"{key}: {field.parts[-1].tolist()[0]}")

        print("\\n--- Tensors ---")
        for i, tensor in enumerate(reader.tensors):
            print(f"[{i}] {tensor.name}, shape: {tensor.shape}, type: {tensor.tensor_type.name}")
        print("---------------------\\n")

    except Exception as e:
        print(f"An error occurred: {e}")

def extract_tensor():
    """Extracts a tensor from a GGUF file."""
    filepath = input("Enter the path to the GGUF file: ")
    if not os.path.exists(filepath):
        print("File not found.")
        return

    try:
        reader = GGUFReader(filepath)
        print("\\n--- Tensors ---")
        for i, tensor in enumerate(reader.tensors):
            print(f"[{i}] {tensor.name}")

        tensor_index = int(input("Enter the index of the tensor to extract: "))
        if not 0 <= tensor_index < len(reader.tensors):
            print("Invalid tensor index.")
            return

        output_path = input("Enter the output path for the .npy file: ")
        np.save(output_path, reader.tensors[tensor_index].data)
        print(f"Tensor saved to {output_path}")

    except Exception as e:
        print(f"An error occurred: {e}")

def create_simple_gguf():
    """Creates a simple GGUF file with a dummy tensor."""
    filepath = input("Enter the output path for the new GGUF file: ")
    try:
        writer = GGUFWriter(filepath, "dummy_arch")

        # Add some metadata
        writer.add_string("general.name", "My Dummy Model")
        writer.add_uint32("general.block_count", 1)

        # Create a dummy tensor
        dummy_tensor = np.random.rand(4, 4).astype(np.float32)
        writer.add_tensor("dummy_tensor", dummy_tensor, raw_dtype=GGMLQuantizationType.F32)

        writer.write_header_to_file()
        writer.write_kv_data_to_file()
        writer.write_tensors_to_file()
        writer.close()

        print(f"GGUF file created at {filepath}")

    except Exception as e:
        print(f"An error occurred: {e}")

def main_menu():
    """Displays the main menu and handles user input."""
    while True:
        print("\\n--- GGUF Interactive Tool ---")
        print("1. View GGUF file information")
        print("2. Extract a tensor from a GGUF file")
        print("3. Create a simple GGUF file")
        print("4. Exit")

        choice = input("Enter your choice: ")

        if choice == '1':
            view_gguf_info()
        elif choice == '2':
            extract_tensor()
        elif choice == '3':
            create_simple_gguf()
        elif choice == '4':
            break
        else:
            print("Invalid choice, please try again.")

if __name__ == "__main__":
    main_menu()
