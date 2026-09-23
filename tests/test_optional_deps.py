"""optional_deps.py: 선택 패키지가 없어도 예외 없이 None을 반환하는지."""

from pst_engine import optional_deps


def test_missing_package_returns_none_without_raising():
    optional_deps._try_import.cache_clear()
    result = optional_deps._try_import("this_package_does_not_exist_at_all_xyz")
    assert result is None


def test_available_summary_returns_bool_dict():
    summary = optional_deps.available_summary()
    assert set(summary.keys()) == {"yaml", "chardet", "rich", "pypff", "extract_msg"}
    assert all(isinstance(v, bool) for v in summary.values())


def test_stdlib_module_is_found():
    # 표준 라이브러리도 같은 경로로 잡히는지(캐시/로직 확인용) — json은
    # 항상 있다.
    optional_deps._try_import.cache_clear()
    result = optional_deps._try_import("json")
    assert result is not None
