#! /usr/bin/env sh

export PYENV_ROOT="${HOME}/.pyenv"
export PATH="${PYENV_ROOT}/bin:$PATH"
eval "$(pyenv init -)"

apt-get install -y python3-venv
pip3 install poetry

mkdir -p ~/.config/pypoetry/
echo "[virtualenvs]" > ~/.config/pypoetry/config.toml
echo "in-project = true" >> ~/.config/pypoetry/config.toml
poetry install
