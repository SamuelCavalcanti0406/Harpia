"""Isola o índice FTS5 dos testes; o índice real em .local/ nunca é tocado.

Importado na primeira linha dos testes (funciona com pytest e com unittest discover).
"""
import os
from pathlib import Path
import sys
import tempfile

_pasta = tempfile.mkdtemp(prefix='harpia_indice_')
os.environ['HARPIA_INDEX_PATH'] = str(Path(_pasta) / 'conhecimento_teste.db')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
