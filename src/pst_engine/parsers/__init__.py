"""PST/EML/MSG 파서 패키지.

각 파서는 :class:`~pst_engine.parsers.base.ParserProtocol`을 만족하며,
필드 정규화 없이 :class:`~pst_engine.models.RawMessage`만 만들어 낸다.
어떤 파서를 쓸지는 ``resolver.py``가 결정한다 — 개별 파서는 자기 자신을
선택하지 않는다.
"""
