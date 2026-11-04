# llm-literature-sieve

`litsieve` is a small engine for LLM-assisted literature reviews. It screens search exports and drafts structured extractions as JSON, validates every response against a schema generated from a YAML field dictionary, and writes an Excel workbook for human verification. Stored LLM drafts are diffed against verified values to measure field-level extraction accuracy.

**Status:** early development; interfaces will change.

## Development install

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
pytest
```

## License

MIT
