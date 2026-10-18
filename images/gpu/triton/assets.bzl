"""Generation-pinned inputs from the existing public Triton image recipe."""

MODEL_FILES = {
    "LICENSE.txt": struct(
        generation = "1761633437922239",
        sha256 = "8c17c2ebb0ea011be9981cc3922db8ca8fa61e828c5d3f44cb6ae342bf80460b",
        size = 7020,
    ),
    "README.md": struct(
        generation = "1761633438049007",
        sha256 = "4f594894ae7523e7ebe1c1039e22dd42c80ea2c5ac7d57325ba27fa07ba3e7ed",
        size = 22332,
    ),
    "USE_POLICY.md": struct(
        generation = "1761633438052185",
        sha256 = "7c23fb36d80141c4ab8cdbb61ee4790102ebd2bf7aeff414453177d4f2110e5d",
        size = 4766,
    ),
    "config.json": struct(
        generation = "1761633438058149",
        sha256 = "c904bd84ff892a2a0a454a35a4031914ccb65a82974ac8ab0fe34d3744656fc2",
        size = 614,
    ),
    "generation_config.json": struct(
        generation = "1761633438059991",
        sha256 = "4eb769d945b7c9a9d9b5ee0cff40ac23a62e218ac683f87ac68d8ad9cd18dd7f",
        size = 188,
    ),
    "model-00001-of-00002.safetensors": struct(
        generation = "1761633504635844",
        sha256 = "66dec18c9f1705b9387d62f8485f4e7d871ca388718786737ed3c72dbfaac9fb",
        size = 9976576152,
    ),
    "model-00002-of-00002.safetensors": struct(
        generation = "1761633474916020",
        sha256 = "0fd6895090da1b2ccffdb93964847709a3b31e6b69fe7dc5a480dce37c811b1d",
        size = 3500296424,
    ),
    "model.safetensors.index.json": struct(
        generation = "1761633437924786",
        sha256 = "11b694a9d4bffdac71733db7f782be0df1f87a65fb9ef05bcf6d317029c22364",
        size = 26788,
    ),
    "pytorch_model-00001-of-00002.bin": struct(
        generation = "1761633510309281",
        sha256 = "aa5ed69a0bc6fb6c520651607f0d857f2aac245e721087d706926cf032887342",
        size = 9976634558,
    ),
    "pytorch_model-00002-of-00002.bin": struct(
        generation = "1761633462590323",
        sha256 = "1de3167f93dcd7c26ac5ad7077dfa987f2bc18e10078ccca6c975b2825c74827",
        size = 3500315539,
    ),
    "pytorch_model.bin.index.json": struct(
        generation = "1761633438064448",
        sha256 = "aca3c1facc89d12311a667c32e85ac86e625990992ea8a189e3b036ba371b931",
        size = 26788,
    ),
    "special_tokens_map.json": struct(
        generation = "1761633438062572",
        sha256 = "6fa06efa2785e450051989a6f8fb4416b10149ded485ddd3f127a40734f5cfd0",
        size = 414,
    ),
    "tokenizer.json": struct(
        generation = "1761633438111787",
        sha256 = "bcd04f0eadf90287bd26e1a183ac487d8a141b09b06aecb7725bbdd343640f2e",
        size = 1842767,
    ),
    "tokenizer.model": struct(
        generation = "1761633437882512",
        sha256 = "9e556afd44213b6bd1be2b850ebbbd98f5481437a8021afaf58ee7fb1818d347",
        size = 499723,
    ),
    "tokenizer_config.json": struct(
        generation = "1761633437929919",
        sha256 = "04aaa6eedd97412f3e63dd2493d2388b82347dd512b89192d3e9b4973a176417",
        size = 1618,
    ),
}

ENGINE_FILES = {
    "fp8/1-gpu/config.json": struct(
        generation = "1761633319077206",
        sha256 = "dd8b9a8160fafe6f7350bbf6e48dacda133be1a54e1f5810814d16c232b5a7e7",
        size = 6911,
    ),
    "fp8/1-gpu/rank0.engine": struct(
        generation = "1761633368442255",
        sha256 = "bbf0fa34fdc446d9dd2ad01abd1b84a0ce0d2f960ef5456d0b4f8ad42f84369c",
        size = 7009414956,
    ),
}
