# Contributing to SNIN Relay V2

## How to Contribute

1. **Fork** the repository
2. **Create a branch** (`git checkout -b feature/your-feature`)
3. **Make changes** with tests
4. **Run tests** (`python3 -m pytest`)
5. **Commit** with clear message
6. **Push** to your fork
7. **Open a Pull Request**

## Code Style

- Python 3.11+ with type hints
- Follow PEP 8 (use `black` formatter)
- Async-first: use `async/await`, not blocking calls
- Docstrings for all public functions
- Logging via Python `logging` module, not `print()`

## Testing

```bash
# Run all tests
python3 -m pytest tests/

# Run specific test
python3 -m pytest tests/test_relay_nips.py -v

# With coverage
python3 -m pytest --cov=.
```

## Adding a New NIP

1. Add validation logic to `relay_server_v2.py`
2. Update `NIP_SUPPORT.md`
3. Write tests in `test_relay_nips.py`
4. Update the relay description to advertise the NIP

## Adding a New Kind

1. Define the kind constant (use high range: 39000+ for custom)
2. Add validation: kind range, required tags
3. Add storage/indexing if needed
4. Document in `SPECIFICATION.md`

## Reporting Issues

Use GitHub Issues with label:
- `bug` — something doesn't work
- `enhancement` — new feature idea
- `question` — how to do X
- `nip` — NIP support request

## Governance

This project is governed by the SNIN DAO. Significant changes to protocol or architecture require a DAO vote.

## License

MIT — by contributing, you agree your contributions are MIT-licensed.
