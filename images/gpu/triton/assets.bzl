"""Generation-pinned inputs from the existing public Triton image recipe."""

MODEL_FILES = {
    "LICENSE.txt": struct(
        generation = "1761633437922239",
        md5 = "4e9e4e6aa7b0912614f9b9f461936288",
        size = 7020,
    ),
    "README.md": struct(
        generation = "1761633438049007",
        md5 = "69dcd8e282f3a95b70fa754a622148f7",
        size = 22332,
    ),
    "USE_POLICY.md": struct(
        generation = "1761633438052185",
        md5 = "74a3732aaed2a0456f741f1fc65ce390",
        size = 4766,
    ),
    "config.json": struct(
        generation = "1761633438058149",
        md5 = "98be520776aa8188d183bb6f107cba17",
        size = 614,
    ),
    "generation_config.json": struct(
        generation = "1761633438059991",
        md5 = "556b851c246f737f7efbbd0e4a53b15a",
        size = 188,
    ),
    "model-00001-of-00002.safetensors": struct(
        generation = "1761633504635844",
        md5 = "1e334597434671c658e20e83c60f8e96",
        size = 9976576152,
    ),
    "model-00002-of-00002.safetensors": struct(
        generation = "1761633474916020",
        md5 = "c804b72b5e4b0aebf13689aee79bdfc8",
        size = 3500296424,
    ),
    "model.safetensors.index.json": struct(
        generation = "1761633437924786",
        md5 = "40ad62b91b88adb33eefd9c6ebf3b882",
        size = 26788,
    ),
    "pytorch_model-00001-of-00002.bin": struct(
        generation = "1761633510309281",
        md5 = "cd2c3018c7e8afd7e8ab0c793d1043fa",
        size = 9976634558,
    ),
    "pytorch_model-00002-of-00002.bin": struct(
        generation = "1761633462590323",
        md5 = "406168ad282eab22c267a6d90eb26fc5",
        size = 3500315539,
    ),
    "pytorch_model.bin.index.json": struct(
        generation = "1761633438064448",
        md5 = "81648ef3915ed2e83d49fed93122d53e",
        size = 26788,
    ),
    "special_tokens_map.json": struct(
        generation = "1761633438062572",
        md5 = "e60ae4706e691cf378545ad0bd042f6f",
        size = 414,
    ),
    "tokenizer.json": struct(
        generation = "1761633438111787",
        md5 = "c9dc953a24ad2b76b4bae4bf456f18bd",
        size = 1842767,
    ),
    "tokenizer.model": struct(
        generation = "1761633437882512",
        md5 = "eeec4125e9c7560836b4873b6f8e3025",
        size = 499723,
    ),
    "tokenizer_config.json": struct(
        generation = "1761633437929919",
        md5 = "26800a83bb73d5226009006a56095e85",
        size = 1618,
    ),
}

ENGINE_FILES = {
    "fp8/1-gpu/config.json": struct(
        generation = "1761633319077206",
        md5 = "231dca4cae77a7160f0a6738d3e187d3",
        size = 6911,
    ),
    "fp8/1-gpu/rank0.engine": struct(
        generation = "1761633368442255",
        md5 = "f25ec137eec7390704ff79245c0dd610",
        size = 7009414956,
    ),
}
