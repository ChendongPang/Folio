"""Readable business research views over canonical, task-scoped tool records."""

from folio.models import digest
from folio.research.presentation import SourcePresentation
from folio.research.transport import research_text


def evidence_catalog_item(item):
    value = {"evidence_id": item.evidence_id, "source_id": item.source_id,
             "locator": item.locator, "preview": item.text[:300]}
    if item.metadata.get("role") == "public_web":
        value.update(metadata={key: item.metadata[key] for key in ("role", "project_fact", "url", "title")
                               if key in item.metadata}, retrieved_at=item.retrieved_at)
    return value


class BusinessPresentation(SourcePresentation):
    def source_label(self, value):
        source = self.file_id(value["source_id"])
        if value.get("metadata", {}).get("role") == "public_web":
            source = ("Public-web reference (not a project fact): " + source
                      + " | " + value["metadata"].get("title", "")
                      + " | retrieved " + str(value.get("retrieved_at") or "unknown"))
        return source

    def evidence_text(self, item):
        value = item.model_dump() if hasattr(item, "model_dump") else item
        return (f"[{self.evidence_id(value['evidence_id'])}] "
                f"{self.source_label(value)} / {value['locator']}\n{value['text']}")

    def evidence_catalog_text(self, items):
        return "\n\n".join(
            f"[{self.evidence_id(item['evidence_id'])}] "
            f"{self.source_label(item)} / {item['locator']}\n"
            f"Preview: {item.get('preview', '')}"
            for item in items
        ) or "No evidence obtained yet."

    def delivered_evidence(self, observation, evidence, delivery_ranges=None):
        """Use program-owned spans; source text may itself contain fake E labels."""
        text = observation.get("model_text", "")
        if observation.get("delivery_complete") is False:
            text = text[:observation["delivered_text_chars"]]
        complete, partial = [], []
        ranges = delivery_ranges if delivery_ranges is not None else {}
        for span in observation.get("result", {}).get("model_evidence_spans", []):
            key = span.get("evidence_id")
            item = evidence.get(key)
            if item is None:
                continue
            start, end = span.get("start"), span.get("end")
            if (type(start) is not int or type(end) is not int or start < 0
                    or end <= start or start >= len(text)):
                continue
            visible = text[start:min(end, len(text))]
            offset = span.get("evidence_offset", 0)
            if (type(offset) is not int or offset < 0
                    or item.text[offset:offset + len(visible)] != visible):
                partial.append(key)
                continue
            identity = digest(repr((item.source_id, item.source_version, item.locator, item.text)))
            previous = ranges.get(key, {})
            segments = previous.get("ranges", []) if previous.get("identity") == identity else []
            merged = []
            for lower, upper in sorted([*segments, [offset, offset + len(visible)]]):
                if merged and lower <= merged[-1][1]:
                    merged[-1][1] = max(upper, merged[-1][1])
                else:
                    merged.append([lower, upper])
            ranges[key] = {"identity": identity, "ranges": merged}
            if len(merged) == 1 and merged[0] == [0, len(item.text)]:
                complete.append(key)
            else:
                partial.append(key)
        return {"delivered_evidence_ids": list(dict.fromkeys(complete)),
                "partially_delivered_evidence_ids": list(dict.fromkeys(partial))}

    def result_text(self, tool, args, result):
        if not isinstance(result, dict) or "error" in result:
            return super().result_text(tool, args, result)
        if tool == "fs.list":
            if args.get("scope") == "artifacts":
                return research_text({"Reusable artifacts": result})
            text = self.catalog_text(result)
            # Script execution addresses the read-only original snapshot. These
            # paths are only sent when the agent asks for a file listing, not
            # repeated in the catalog attached to every research decision.
            paths = [f"- {self.file_id(item['path'])} → {item['path']}"
                     for item in result.get("files", [])]
            return text + ("\n\nOriginal relative paths for script access:\n" + "\n".join(paths) if paths else "")
        if tool == "fs.read" and args.get("scope") in {"history", "evidence"} and "items" in result:
            if args["scope"] == "evidence":
                text = self.evidence_catalog_text(result["items"])
            else:
                text = "\n".join(f"- {item['request_id']}：{item['tool'].replace('.', '_')} / {item['status']}"
                                 for item in result["items"])
            return text + "\n\n" + self._continuation(tool, args, result)
        if (tool in {"fs.inspect", "fs.search"}
                or tool == "fs.read" and ("evidence" in result or args.get("scope", "sources") != "sources")):
            return super().result_text(tool, args, result)
        value = {key: item for key, item in result.items()
                 if key not in {"page_images", "model_text", "model_evidence_spans", "evidence"}}
        text = research_text({"Tool result": value})
        spans = []
        for item in result.get("evidence", []):
            block = self.evidence_text(item)
            text += "\n\n" + block
            spans.append({"evidence_id": item["evidence_id"],
                          "start": len(text) - len(item["text"]), "end": len(text)})
        if spans:
            result["model_evidence_spans"] = spans
        return text

    def progress_text(self, progress):
        lines = ["Program extraction progress follows. Actual supplied content appears in tool results; extraction does not establish that all body text was read."]
        for item in progress:
            args = item["arguments"]
            source = self.file_id(args.get("path", args.get("file", "")))
            node = args.get("node_id", args.get("node", ""))
            if node and args.get("path") in self.files:
                node = self.node_id(args["path"], node)
            state = "Extracted through the end of the range" if item["pagination_complete"] else f"More content remains, offset={item['next_offset']}"
            if not item["pagination_complete"] and item.get("next_character_offset"):
                state += f", character_offset={item['next_character_offset']}"
            lines.append(f"- {item['tool'].replace('.', '_')} {source} {node}：{state}; record {item['request_id']}")
            if not item["pagination_complete"] and item.get("next_offset") is not None:
                lines[-1] += "; continue: " + self.continuation_command(
                    item["tool"], args, item["next_offset"], item.get("next_character_offset") or 0)
        return "\n".join(lines)
