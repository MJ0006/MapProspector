"""Ask (F3): plain-English questions about your data, answered by Claude through the
MapProspector MCP server with read-only tools ("who owns the north side for roof repair?")."""
from fastapi import APIRouter, Form, Request

from app import ai, brain
from app.mcp_server import READ_ONLY
from app.web import render

router = APIRouter()
EXAMPLES = [
    "Which of my clients lost ground in their latest heat maps?",
    "For my newest scan, which competitor owns the most top-3 spots and on which side of town?",
    "Which clients have reviews waiting for a reply?",
    "Summarise the Local SEO Brain roadmap for my first client in 5 bullet points.",
]


@router.get("/ask")
def ask_page(request: Request):
    return render(request, "ask.html", title="Ask", examples=EXAMPLES, answer=None, question="")


@router.post("/ask")
def ask_post(request: Request, question: str = Form(...)):
    prompt = ("You are an assistant inside MapProspector, a local SEO app. Answer using the mapprospector tools "
              "(read-only). Cite scan ids, client names and numbers you used. Say plainly when data is missing. "
              "Do not claim why a competitor ranks; describe the data.\n\n" + brain.GUARDRAILS +
              f"\n\nQuestion: {question}")
    answer, error = None, ""
    try:
        answer = ai.ask(prompt, mcp=True, tools=[f"mcp__mapprospector__{t}" for t in READ_ONLY], timeout=300)
    except ai.AIUnavailable as exc:
        error = str(exc)
    return render(request, "ask.html", title="Ask", examples=EXAMPLES, answer=answer, error=error, question=question)
