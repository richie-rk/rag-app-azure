"""Citation extraction and formatting.

Parses [filename.pdf] references from LLM output and maps them to source documents.
Also extracts follow-up questions wrapped in <<...>>.
"""

import re


def extract_citations(llm_response: str, data_points: list[dict]) -> list[str]:
    """Extract unique [filename] citations from LLM output.

    Returns list of filenames that were cited.
    """
    pattern = r"\[([^\]]+)\]"
    cited = re.findall(pattern, llm_response)

    known_files = {dp.get("sourcefile", "") for dp in data_points}
    known_pages = {dp.get("sourcepage", "") for dp in data_points}

    return list(
        dict.fromkeys(
            c for c in cited if c in known_files or c in known_pages
        )
    )


def extract_followup_questions(content: str) -> tuple[str, list[str]]:
    """Extract follow-up questions from <<...>> markers in LLM output.

    Returns (clean_content, list_of_questions) where clean_content has
    the follow-up section removed.
    """
    questions = re.findall(r"<<([^>]+)>>", content)

    # Remove everything from the first << onward
    first_marker = content.find("<<")
    if first_marker > 0:
        clean = content[:first_marker].rstrip()
    else:
        clean = content

    return clean, questions
