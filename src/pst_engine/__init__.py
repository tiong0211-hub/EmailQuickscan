"""EmailQuickscan — 사내망 Windows용 PST 메일 검색 엔진.

이 패키지는 순수 Python + 표준 SQLite(FTS5)만으로 동작하도록 설계되었다.
가속 파서(pypff, extract_msg)와 선택 라이브러리(PyYAML, chardet, rich)는
빌드 시점에 실행 파일에 번들되며, 런타임에는 있으면 쓰고 없으면 표준
라이브러리 기반 폴백으로 동작한다. 자세한 설계 근거는 프로젝트 루트의
README.md와 CLAUDE.md를 참조한다.
"""

__version__ = "0.1.0"
