from setuptools import setup, find_packages

setup(
    name="nostr-sse-client",
    version="1.0.0",
    description="Nostr client over SSE (Server-Sent Events) — no WebSocket required",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="SNIN Network",
    url="https://github.com/snin/nostr-sse-client",
    packages=find_packages(),
    install_requires=[
        "requests>=2.28",
        "coincurve>=18.0",
        "nostr_protocol>=0.1.0",
        "bech32>=1.2",
    ],
    entry_points={
        "console_scripts": [
            "nostr-sse=nostr_sse_client.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Communications :: Chat",
        "Topic :: Internet :: WWW/HTTP",
    ],
    python_requires=">=3.10",
)
