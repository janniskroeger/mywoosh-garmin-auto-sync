"""Builds minimal Zwift .zwo files for tests."""


def make_zwo(name: str, blocks: list[str]) -> bytes:
    """A .zwo with the given name and raw XML block elements."""
    body = "\n".join(blocks)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<workout_file><author>open-tri</author><name>{name}</name><sportType>bike</sportType>"
        f"<workout>\n{body}\n</workout></workout_file>"
    )
    return xml.encode("utf-8")
