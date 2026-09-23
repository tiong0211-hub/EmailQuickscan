"""config.py: PyYAML 경로와 표준 라이브러리 서브셋 파서 경로가 같은
결과를 내는지, 그리고 설정 파일이 없거나 손상돼도 안전한 기본값으로
동작하는지 검증한다.
"""

import sys
from pathlib import Path

import pytest

from pst_engine import config as config_mod
from pst_engine.config import Config, _default_yaml_path, load_config, parse_yaml_subset

_REAL_YAML = (Path(__file__).resolve().parent.parent / "config" / "default.yaml").read_text(encoding="utf-8")


def test_subset_parser_matches_pyyaml_on_real_config():
    yaml = pytest.importorskip("yaml")
    subset_result = parse_yaml_subset(_REAL_YAML)
    pyyaml_result = yaml.safe_load(_REAL_YAML)
    assert subset_result == pyyaml_result


def test_subset_parser_handles_nesting_lists_comments_and_null():
    text = """
# 주석
a:
  b: 1
  c:
    - 0.5
    - 1
    - 2
  d: null
e: "quoted value"
f: true
g: 3.5
"""
    result = parse_yaml_subset(text)
    assert result == {
        "a": {"b": 1, "c": [0.5, 1, 2], "d": None},
        "e": "quoted value",
        "f": True,
        "g": 3.5,
    }


def test_load_config_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "does_not_exist.yaml")
    assert cfg.batch_size == 1000
    assert cfg.fts_tokenize == "unicode61 remove_diacritics 0"
    assert cfg.fts_detail == "column"


def test_load_config_corrupted_file_does_not_raise(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("this: is: not: valid: yaml: [", encoding="utf-8")
    cfg = load_config(bad)  # 예외 없이 기본값으로 폴백해야 한다
    assert isinstance(cfg, Config)
    assert cfg.batch_size == 1000


def test_load_config_partial_override(tmp_path):
    p = tmp_path / "partial.yaml"
    p.write_text("indexing:\n  batch_size: 42\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.batch_size == 42
    # 나머지는 기본값 유지
    assert cfg.snippet_len == 300


def test_load_config_without_pyyaml_falls_back(monkeypatch, tmp_path):
    """PyYAML이 없는 것처럼 흉내내도 서브셋 파서로 정상 로드돼야 한다."""
    p = tmp_path / "cfg.yaml"
    p.write_text("indexing:\n  batch_size: 7\n  workers: 2\n", encoding="utf-8")

    monkeypatch.setattr(config_mod.optional_deps, "get_yaml", lambda: None)
    cfg = load_config(p)
    assert cfg.batch_size == 7
    assert cfg.workers == 2


def test_default_yaml_path_dev_mode_uses_repo_config(monkeypatch):
    """개발 환경(sys.frozen 없음)에서는 소스 파일 기준 저장소 루트의
    config/default.yaml을 가리켜야 한다."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    path = _default_yaml_path()
    assert path == Path(config_mod.__file__).resolve().parent.parent.parent / "config" / "default.yaml"
    assert path.name == "default.yaml"


def test_default_yaml_path_frozen_prefers_folder_next_to_exe(monkeypatch, tmp_path):
    """PyInstaller onefile(sys.frozen=True)에서는 exe가 놓인 폴더의
    config/default.yaml을 최우선으로 찾아야 한다 — 관리자가 재빌드 없이
    exe 옆에 이 파일을 두면 설정을 바꿀 수 있게 하려는 것(실제 배포
    시나리오에서 중요한 기능이다).
    """
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    fake_exe = exe_dir / "EmailQuickscan.exe"
    fake_exe.write_bytes(b"")
    external_cfg_dir = exe_dir / "config"
    external_cfg_dir.mkdir()
    (external_cfg_dir / "default.yaml").write_text("indexing:\n  batch_size: 999\n", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)

    path = _default_yaml_path()
    assert path == external_cfg_dir / "default.yaml"

    cfg = load_config(path)
    assert cfg.batch_size == 999


def test_default_yaml_path_frozen_falls_back_to_bundled_meipass(monkeypatch, tmp_path):
    """exe 옆에 config/default.yaml이 없으면(일반적인 경우) 번들 안에
    동봉된(_MEIPASS) 기본값으로 폴백해야 한다.
    """
    exe_dir = tmp_path / "dist"
    exe_dir.mkdir()
    fake_exe = exe_dir / "EmailQuickscan.exe"
    fake_exe.write_bytes(b"")
    meipass = tmp_path / "meipass_tmp"
    meipass.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    path = _default_yaml_path()
    assert path == meipass / "config" / "default.yaml"
