"""Append-only market facts and source objects, separate from the account ledger."""

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.config import local_path
from china_stock_sim.market.contracts import aware_time
from china_stock_sim.market.storage import digest, read_json, write_json

KINDS = {"bar", "index", "calendar", "security", "event", "event_batch", "coverage", "factor", "pool"}


class CorruptData(ValueError):
    pass


def timestamp() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


class MarketRepository:
    """Construction and reading never create files or call a provider."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.path = local_path(self.root, "data/state/market.sqlite3")
        self.objects = local_path(self.root, "data/raw/market")
        self.snapshots = local_path(self.root, "data/normalized/snapshots")

    @contextmanager
    def writer(self):
        lock = local_path(self.root, "data/state/market.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("已有数据更新任务运行") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    @contextmanager
    def connection(self, *, write=False):
        if not write and not self.path.is_file():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path)
        else:
            connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            schema = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema not in ({0, 1} if write else {1}):
                raise CorruptData("不支持的数据索引格式")
            if write:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS observations (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, record_key TEXT NOT NULL,
                        code TEXT NOT NULL, session TEXT NOT NULL, available_at TEXT NOT NULL,
                        fetched_at TEXT NOT NULL, provider TEXT NOT NULL, raw_ref TEXT NOT NULL,
                        payload TEXT NOT NULL, checksum TEXT NOT NULL,
                        UNIQUE(kind,record_key,id)
                    );
                    CREATE INDEX IF NOT EXISTS observation_lookup
                        ON observations(kind,code,session,available_at);
                    CREATE TABLE IF NOT EXISTS requests (
                        request_id TEXT PRIMARY KEY, detail TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY, detail TEXT NOT NULL
                    );
                    PRAGMA user_version=1;
                """)
            elif connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise CorruptData("不支持的数据索引格式")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def save_raw(self, response: dict) -> str:
        key = digest(response)
        path = self.objects / f"{key}.json"
        if path.exists():
            try:
                if digest(read_json(path)) != key:
                    raise CorruptData("原始对象内容损坏")
            except (ValueError, OSError) as exc:
                raise CorruptData(f"原始对象损坏: {path.name}") from exc
        else:
            write_json(path, response)
        return str(path.relative_to(self.root))

    def validate_raw(self, reference: str, _seen=None) -> None:
        seen = set() if _seen is None else _seen
        if reference in seen:
            return
        seen.add(reference)
        path = local_path(self.root, reference)
        if not path.is_relative_to(self.objects):
            raise CorruptData("原始对象不在本项目数据对象目录内")
        try:
            content = read_json(path)
            if digest(content) != path.stem:
                raise CorruptData("原始对象摘要不符")
            for dependency in content.get("dependencies", []):
                self.validate_raw(dependency, seen)
        except (OSError, ValueError) as exc:
            raise CorruptData(f"源文件缺失或损坏: {reference}") from exc

    def put(self, kind: str, records: list[dict], response: dict, *, key_fields=None,
            connection=None) -> list[str]:
        """Append facts; an explicit connection joins the caller's atomic transaction."""
        if kind not in KINDS:
            raise ValueError("未支持的数据类别")
        fetched = aware_time(response["fetched_at"]).isoformat()
        provider = response["provider"]
        raw_ref = self.save_raw(response)
        entries = []
        for record in records:
            payload = dict(record)
            code = str(payload.get("code", ""))
            session = str(payload.get("date", payload.get("ex_date") or ""))
            available = max(aware_time(fetched), aware_time(payload.get("available_at", fetched)))
            available = available.astimezone(ZoneInfo("UTC")).isoformat()
            fields = key_fields or (["event_id"] if kind == "event" else ["code", "date"])
            identity = {field: payload.get(field, "") for field in fields}
            key = digest(identity)
            checksum = digest(payload)
            record_id = digest({"kind": kind, "key": key, "payload": payload,
                                "provider": provider, "raw_ref": raw_ref, "available_at": available})
            entries.append((record_id, kind, key, code, session, available, fetched, provider,
                            raw_ref, json.dumps(payload, ensure_ascii=False, allow_nan=False), checksum))
        if connection is None:
            with self.connection(write=True) as owned:
                owned.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                  entries)
        else:
            connection.executemany("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   entries)
        return [entry[0] for entry in entries]

    def _decode(self, row, *, verify=True, verified=None) -> dict:
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        if digest(value["payload"]) != value["checksum"]:
            raise CorruptData(f"数据索引内容摘要不符: {value['id']}")
        expected_id = digest({"kind": value["kind"], "key": value["record_key"],
                              "payload": value["payload"], "provider": value["provider"],
                              "raw_ref": value["raw_ref"], "available_at": value["available_at"]})
        if expected_id != value["id"] or aware_time(value["available_at"]) < aware_time(value["fetched_at"]):
            raise CorruptData("数据索引身份或知识时点损坏")
        expected_code = str(value["payload"].get("code", ""))
        expected_session = str(value["payload"].get("date", value["payload"].get("ex_date") or ""))
        if value["code"] != expected_code or value["session"] != expected_session:
            raise CorruptData("数据索引证券日期与事实不符")
        if verify:
            verified = {} if verified is None else verified
            if value["raw_ref"] not in verified:
                self.validate_raw(value["raw_ref"])
                verified[value["raw_ref"]] = read_json(local_path(self.root, value["raw_ref"]))
            raw = verified[value["raw_ref"]]
            if (aware_time(raw["fetched_at"]) != aware_time(value["fetched_at"])
                    or raw["provider"] != value["provider"]):
                raise CorruptData("数据取得时间或来源与原件不符")
            if value["kind"] == "event":
                self._validate_tax_sources(value, raw, verified)
        return value

    def _validate_tax_sources(self, observation: dict, raw: dict, verified: dict) -> None:
        """Bind supplementary facts to immutable evidence, including snapshot reads.

        An adapter may cite its own raw response or a declared direct dependency.
        A nonempty path alone is never proof that another object was available.
        Old event payloads without supplementary fields remain valid and are
        normalized to explicit unknowns by the public event reader.
        """
        from china_stock_sim.market.quality import normalize_code, normalize_date
        from china_stock_sim.market.tax_facts import normalize_event_tax_facts, normalize_tax_value

        try:
            # Validate a copy; immutable stored payloads and their IDs stay intact.
            normalized = normalize_event_tax_facts(observation["payload"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptData("税基事实结构或字段证据无效") from exc
        facts = normalized["tax_facts"]
        if not isinstance(facts, dict):
            raise CorruptData("税基事实结构无效")
        allowed = {observation["raw_ref"], *raw.get("dependencies", [])}
        payload = observation["payload"]
        aliases = {payload.get("event_id"), *(item.get("event_id")
                   for item in payload.get("evidence", []) if isinstance(item, dict))}
        for field, fact in facts.items():
            if not isinstance(fact, dict) or not isinstance(fact.get("sources", []), list):
                raise CorruptData("税基事实来源结构无效")
            for source in fact.get("sources", []):
                if not isinstance(source, dict):
                    raise CorruptData("税基事实来源结构无效")
                reference = source.get("raw_reference")
                if not isinstance(reference, str) or reference not in allowed:
                    raise CorruptData("税基事实引用未绑定的原件")
                if reference not in verified:
                    self.validate_raw(reference)
                    verified[reference] = read_json(local_path(self.root, reference))
                evidence = verified[reference]
                try:
                    if evidence["provider"] != source.get("provider"):
                        raise CorruptData("税基事实来源与原件提供方不符")
                    if aware_time(evidence["fetched_at"]) > aware_time(observation["available_at"]):
                        raise CorruptData("税基事实引用晚于可用时点的原件")
                    # An intact document alone cannot prove a field. A canonical
                    # adapter must cite one exact row with matching identity/date
                    # and a directly checkable value; guessing a row is forbidden.
                    rows, index = evidence.get("rows"), source.get("row_index")
                    if (not isinstance(rows, list) or type(index) is not int
                            or not 0 <= index < len(rows) or not isinstance(rows[index], dict)):
                        raise CorruptData("税基事实缺少有效原件行定位")
                    source_row = rows[index]
                    if normalize_code(source_row.get("code")) != payload.get("code"):
                        raise CorruptData("税基事实原件证券不匹配")
                    if (source_row.get("event_id") != source.get("event_id")
                            or source.get("event_id") not in aliases):
                        raise CorruptData("税基事实原件事件不匹配")
                    applicable = normalize_date(source_row.get("applicable_date"))
                    if (applicable != source.get("applicable_date")
                            or (payload.get("ex_date") and applicable != payload["ex_date"])):
                        raise CorruptData("税基事实原件适用日期不匹配")
                    raw_field = source.get("source_field")
                    if not isinstance(raw_field, str) or raw_field not in source_row:
                        raise CorruptData("税基事实原件中缺少所引字段")
                    if normalize_tax_value(field, source_row[raw_field]) != source.get("value"):
                        raise CorruptData("税基事实与原件字段值不匹配")
                except (KeyError, TypeError, ValueError) as exc:
                    if isinstance(exc, CorruptData):
                        raise
                    raise CorruptData("税基事实原件的来源或时点无效") from exc

    def select(self, kind: str, *, code=None, start=None, end=None, cutoff=None,
               verify=True, latest=True) -> list[dict]:
        clauses, parameters = ["kind=?"], [kind]
        for expression, value in (("code=?", code), ("session>=?", start), ("session<=?", end)):
            if value is not None:
                clauses.append(expression)
                parameters.append(value)
        if cutoff is not None:
            clauses.append("available_at<=?")
            parameters.append(aware_time(cutoff).astimezone(ZoneInfo("UTC")).isoformat())
        with self.connection() as connection:
            if connection is None:
                return []
            rows = connection.execute("SELECT * FROM observations WHERE " + " AND ".join(clauses)
                                      + " ORDER BY available_at DESC,rowid DESC", parameters).fetchall()
        seen, result, verified = set(), [], {}
        for row in rows:
            if latest and row["record_key"] in seen:
                continue
            seen.add(row["record_key"])
            result.append(self._decode(row, verify=verify, verified=verified))
        return sorted(result, key=lambda row: (row["code"], row["session"], row["id"]))

    def by_ids(self, identities: list[str]) -> list[dict]:
        with self.connection() as connection:
            if connection is None and identities:
                raise CorruptData("快照依赖的数据索引缺失")
            result, verified = [], {}
            for identity in identities:
                row = connection.execute("SELECT * FROM observations WHERE id=?", (identity,)).fetchone()
                if row is None:
                    raise CorruptData(f"快照事实缺失: {identity}")
                result.append(self._decode(row, verified=verified))
            return result

    def save_state(self, table: str, identity: str, detail: dict):
        if table not in {"requests", "jobs"}:
            raise ValueError("未知任务表")
        column = "request_id" if table == "requests" else "job_id"
        with self.connection(write=True) as connection:
            connection.execute(f"INSERT INTO {table} VALUES (?,?) ON CONFLICT({column}) "
                               "DO UPDATE SET detail=excluded.detail",
                               (identity, json.dumps(detail, ensure_ascii=False)))
        if table == 'requests':
            # A supplementary event after the original commit, never a new recovery authority.
            from china_stock_sim.runtime.recording import emit_event

            source = detail.get('identity', {})
            source = source if isinstance(source, dict) else {}
            emit_event('data_request', request_id=identity, source=source.get('source'),
                category=source.get('query'), status=detail.get('state'),
                error_code=detail.get('category') or detail.get('pause_reason'),
                **{key: detail[key] for key in ('attempts', 'retry_failures', 'started_at',
                    'finished_at', 'next_retry_at', 'server_retry_at') if key in detail})

    def states(self, table: str) -> dict:
        if table not in {"requests", "jobs"}:
            raise ValueError("未知任务表")
        with self.connection() as connection:
            if connection is None:
                return {}
            return {row[0]: json.loads(row[1]) for row in connection.execute(f"SELECT * FROM {table}")}

    def freeze(self, content: dict, observation_ids: list[str]) -> dict:
        self.by_ids(observation_ids)
        body = {"schema": 1, "content": content, "observation_ids": sorted(set(observation_ids))}
        snapshot_id = digest(body)
        envelope = {"snapshot_id": snapshot_id, "body": body}
        path = self.snapshots / f"{snapshot_id}.json"
        if path.exists():
            self.read_snapshot(snapshot_id)
        else:
            write_json(path, envelope)
        return envelope

    def read_snapshot(self, snapshot_id: str) -> dict:
        if len(snapshot_id) != 64 or any(c not in "0123456789abcdef" for c in snapshot_id):
            raise ValueError("快照 ID 无效")
        try:
            envelope = read_json(self.snapshots / f"{snapshot_id}.json")
        except (OSError, ValueError) as exc:
            raise CorruptData("快照文件缺失或损坏") from exc
        if envelope.get("snapshot_id") != snapshot_id or digest(envelope.get("body")) != snapshot_id:
            raise CorruptData("快照摘要不符")
        self.by_ids(envelope["body"]["observation_ids"])
        return envelope

    def quarantine(self, reference: str, *, reason: str) -> dict:
        """Explicit repair; preserve bad bytes for diagnosis, never rewrite old facts."""
        path = local_path(self.root, reference)
        if not path.is_relative_to(self.objects):
            raise ValueError("只能隔离原始数据对象")
        if not path.exists():
            return {"state": "MISSING", "reference": reference}
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        destination = local_path(self.root, f"data/raw/quarantine/{path.stem}-{raw_hash}.json")
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
        record = {"reference": reference, "quarantine": str(destination.relative_to(self.root)),
                  "reason": reason, "at": timestamp()}
        write_json(destination.with_suffix(".reason.json"), record)
        return record
