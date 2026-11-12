"""Transkript -> özet/kararlar/görevler. LLM çağrıları Hermes'in kendi modeliyle (ctx.llm)."""
import re
from datetime import date

SCHEMA = {
    "type": "object",
    "additionalProperties": False,  # Anthropic yapılandırılmış çıktı bunu zorunlu tutar
    "properties": {
        "summary": {"type": "string", "description": "3-8 cümlelik toplantı özeti"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {"type": "string"},
                    "owner": {"type": "string", "description": "Sorumlu kişi; belirtilmediyse boş"},
                    "due_text": {"type": "string", "description": "Süreyi transkriptte söylendiği gibi yaz; yoksa boş"},
                    "due_iso": {"type": "string", "description": "YYYY-MM-DD; yalnızca kesin hesaplanabiliyorsa, yoksa boş"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["summary", "decisions", "tasks"],
}

SYSTEM = (
    "Sen Türkçe toplantı kayıtlarını işleyen titiz bir asistansın. Yalnızca transkriptte geçen bilgiyi kullan; "
    "uydurma. Kişi adlarını, sayıları, tarihleri ve gün adlarını transkriptteki gibi koru. Transkript otomatik "
    "yazıya çevrildiği için bozuk kelimeler olabilir; bağlamdan anlam çıkar ama emin değilsen olduğu gibi bırak. "
    "Yanıtın tamamı Türkçe olsun."
)

NOTES_PROMPT = (
    "Aşağıdaki, uzun bir toplantı kaydının {i}/{n}. parçası. Konuşulan konuları, alınan kararları ve "
    "üstlenilen görevleri (kim, ne, ne zamana kadar) madde madde ve eksiksiz çıkar. Yorum ekleme.\n\n{text}"
)


def chunk_text(text: str, size: int) -> list:
    chunks, cur, n = [], [], 0
    for line in text.splitlines():
        if n + len(line) > size and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def summarize(llm, transcript: str, chunk_chars: int = 14000, title: str = "", on_stage=None) -> dict:
    """Döner: {summary, topics, decisions, tasks[], raw?}"""
    stage = on_stage or (lambda s: None)
    chunks = chunk_text(transcript, chunk_chars)
    if len(chunks) > 1:
        notes = []
        for i, c in enumerate(chunks, 1):
            stage(f"not çıkarılıyor {i}/{len(chunks)}")
            r = llm.complete(
                messages=[{"role": "system", "content": SYSTEM},
                          {"role": "user", "content": NOTES_PROMPT.format(i=i, n=len(chunks), text=c)}],
                temperature=0.1, max_tokens=2500, purpose="meeting-notes.chunk")
            notes.append(f"## Parça {i}\n{r.text}")
        material = "\n\n".join(notes)
        kind = "uzun bir toplantının parça notları"
    else:
        material, kind = transcript, "toplantı transkripti"

    stage("özet ve görevler çıkarılıyor")
    instructions = (
        f"{SYSTEM}\n\nBugünün tarihi: {date.today().isoformat()}. Aşağıda {kind} var"
        + (f" (başlık: {title})" if title else "")
        + ". Özet, alınan kararlar ve yapılacaklar listesini çıkar. Aynı görevi tekrarlama."
    )
    try:
        res = llm.complete_structured(
            instructions=instructions, input=[{"type": "text", "text": material}],
            json_schema=SCHEMA, schema_name="meeting_summary", temperature=0.1, max_tokens=4000,
            purpose="meeting-notes.final")
        parsed = res.parsed
    except Exception:  # sağlayıcı şemayı reddederse serbest metne düş
        parsed = None
    if parsed:
        parsed.setdefault("topics", [])
        return parsed
    # Yedek: serbest metin iste, görevleri satırlardan ayıkla
    r = llm.complete(
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": instructions + "\n\nBiçim:\n## Özet\n## Kararlar\n- ...\n## Yapılacaklar\n- [ ] ...\n\n" + material}],
        temperature=0.1, max_tokens=4000, purpose="meeting-notes.final-fallback")
    tasks = [{"action": m.group(1).strip()} for m in re.finditer(r"^\s*-\s*\[\s\]\s*(.+)$", r.text, re.M)]
    return {"summary": r.text, "topics": [], "decisions": [], "tasks": tasks}


def render_markdown(title: str, data: dict) -> str:
    out = [f"# {title}", "", "## Özet", data.get("summary", "").strip(), ""]
    if data.get("topics"):
        out += ["## Konular"] + [f"- {t}" for t in data["topics"]] + [""]
    out += ["## Kararlar"] + ([f"- {d}" for d in data.get("decisions", [])] or ["- Yok"]) + [""]
    out += ["## Yapılacaklar"]
    for t in data.get("tasks", []):
        extra = ", ".join(x for x in (t.get("owner"), t.get("due_text")) if x)
        out.append(f"- [ ] {t['action']}" + (f" ({extra})" if extra else ""))
    if not data.get("tasks"):
        out.append("- Yok")
    return "\n".join(out) + "\n"
