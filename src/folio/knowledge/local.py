"""Offline lexical retrieval with immutable evidence snapshots.

Knowledge providers implement search(); an external knowledge adapter can implement the same
contract once its API and authorization contract are available.
"""

import math
import re
from collections import Counter, OrderedDict
from pathlib import Path
from threading import Event, RLock
from typing import Protocol

from folio.models import Evidence
from folio.research.formats import SUPPORTED_SUFFIXES
from folio.research.source_index import (
    SourceIndex,
    _check_cancelled,
    cached_source,
    get_prepared_index,
)
from folio.research.workspace import _chunks, _slice_evidence


def terms(text: str) -> list[str]:
    result = re.findall(r"[a-z0-9_]+", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return result


class KnowledgeProvider(Protocol):
    async def search(self, queries: list[str], *, limit: int, max_chars: int) -> list[Evidence]: ...


class LocalKnowledge:
    # Immutable lexical data can be shared between retries and business tasks.
    # Bound the retained size by characters as well as entries, not file count.
    _lexical_cache = OrderedDict()
    _cache_lock = RLock()
    _cache_characters = 0
    _cache_limit = 8_000_000

    def __init__(self, directory: Path, *, index: SourceIndex | None = None, cache_root: Path | None = None, cancelled: Event | None = None):
        directory = Path(directory)
        if index is not None and index.source_root.resolve() != directory.resolve():
            raise ValueError('The source index does not match the original source directory.')
        # Legacy direct callers remain read-only. New job paths explicitly
        # opt into the shared source cache and durable per-snapshot index.
        self.index = index or (
            get_prepared_index(directory, cache_root=cache_root, cancelled=cancelled)
            if cache_root is not None else None
        )
        self.chunks: list[Evidence] = []
        if self.index is not None:
            if not self.index.prepared:
                self.index.prepare(cancelled=cancelled)
            cache_key = (str(self.index.source_root), self.index.source_snapshot_id, self.index.manifest["reader_version"])
            # Explicitly prepared indexes still validate their sources before
            # reusing lexical content; stale objects never adopt changed files.
            for source in self.index.files:
                _check_cancelled(cancelled)
                self.index.validate(source)
            with self._cache_lock:
                cached = self._lexical_cache.get(cache_key)
                if cached is not None:
                    self._lexical_cache.move_to_end(cache_key)
                    chunks, tokens, frequencies, _ = cached
                    self.chunks = [item.model_copy(deep=True) for item in chunks]
                    self.tokens = [Counter(item) for item in tokens]
                    self.df = Counter(frequencies)
                    return
            for source in sorted(self.index.files):
                _check_cancelled(cancelled)
                if Path(source).suffix.lower() in SUPPORTED_SUFFIXES:
                    self.chunks.extend(_chunks(source, self.index.load(source, cancelled=cancelled)))
        else:
            for path in sorted(directory.rglob("*")):
                _check_cancelled(cancelled)
                if path.is_symlink():
                    raise ValueError('Source snapshots cannot contain symbolic links.')
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                document = cached_source(path, cache_root, cancelled=cancelled)
                source = path.relative_to(directory).as_posix()
                self.chunks.extend(_chunks(source, document))
        self.tokens = []
        for evidence in self.chunks:
            _check_cancelled(cancelled)
            self.tokens.append(Counter(terms(evidence.text)))
        self.df = Counter(t for counts in self.tokens for t in counts)
        _check_cancelled(cancelled)
        if self.index is not None:
            size = sum(len(e.text) for e in self.chunks)
            cls = type(self)
            if size <= cls._cache_limit:
                with self._cache_lock:
                    previous = self._lexical_cache.pop(cache_key, None)
                    if previous is not None:
                        cls._cache_characters -= previous[3]
                    self._lexical_cache[cache_key] = (
                        tuple(item.model_copy(deep=True) for item in self.chunks),
                        tuple(Counter(item) for item in self.tokens), Counter(self.df), size,
                    )
                    cls._cache_characters += size
                    while cls._cache_characters > cls._cache_limit or len(self._lexical_cache) > 16:
                        _, removed = self._lexical_cache.popitem(last=False)
                        cls._cache_characters -= removed[3]

    async def search(self, queries: list[str], *, limit: int, max_chars: int) -> list[Evidence]:
        query = set(terms(" ".join(queries)))
        scores = []
        for evidence, counts in zip(self.chunks, self.tokens, strict=True):
            score = sum(
                (1 + math.log(counts[t])) * math.log(1 + len(self.chunks) / (1 + self.df[t]))
                for t in query
                if counts[t]
            )
            if score > 0:
                scores.append((score, evidence))
        result, size = [], 0
        for _, evidence in sorted(scores, key=lambda x: (-x[0], x[1].evidence_id)):
            if len(result) >= limit or size >= max_chars:
                break
            remaining = max_chars - size
            if len(evidence.text) > remaining:
                # Long source paragraphs are no longer pre-split. Return an
                # accurately located excerpt around a hit instead of dropping
                # the paragraph merely because it exceeds this query's budget.
                matches = [match.start() for term in query
                           if (match := re.search(re.escape(term), evidence.text, re.IGNORECASE))]
                hit = min(matches, default=0)
                start = min(max(0, hit - remaining // 4), len(evidence.text) - remaining)
                evidence = _slice_evidence(evidence, start, start + remaining)
            result.append(evidence)
            size += len(evidence.text)
        return result


class EmptyKnowledge:
    async def search(self, queries: list[str], *, limit: int, max_chars: int) -> list[Evidence]:
        return []
