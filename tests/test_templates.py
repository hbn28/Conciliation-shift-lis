from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def test_resultado_template_compila():
    templates_dir = Path(__file__).parents[1] / "app" / "templates"
    environment = Environment(loader=FileSystemLoader(templates_dir))
    environment.filters["money"] = str
    environment.get_template("resultado.html")
