import json
import logging
import os
import subprocess
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, nodes
from jinja2.exceptions import TemplateNotFound, TemplateRuntimeError
from jinja2.ext import Extension

from spacemk import is_command_available


class RaiseExtension(Extension):
    tags = set(["raise"])  # noqa: RUF012

    def parse(self, parser):
        lineno = next(parser.stream).lineno
        message_node = parser.parse_expression()

        return nodes.CallBlock(self.call_method("_raise", [message_node], lineno=lineno), [], [], [], lineno=lineno)

    def _raise(self, msg, caller):  # noqa: ARG002
        raise TemplateRuntimeError(msg)


class Generator:
    def __init__(self, config, console):
        self._config = config
        self._console = console

    def _filter_randomsuffix(self, value: str) -> str:
        return f"{value}_{os.urandom(8).hex()}"

    def _filter_totf(self, value: any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _format_code(self) -> None:
        if not is_command_available("terraform"):
            logging.warning("Terraform is not installed. Skipping generated Terraform code formatting.")
            return

        code_folder_path = Path(f"{__file__}/../../tmp/code").resolve()
        process = subprocess.run(
            f"terraform fmt -no-color {code_folder_path}", capture_output=True, check=False, shell=True, text=True
        )

        if process.returncode != 0:
            logging.warning(f"Could not format generated Terraform code: {process.stderr}")
        else:
            logging.info("Formatted generated Terraform code")

    def _generate_code(self, data: dict):
        env = Environment(
            autoescape=False,
            extensions=[RaiseExtension],
            loader=ChoiceLoader(
                [
                    FileSystemLoader(Path(f"{Path(__file__).parent.resolve()}/../../custom/templates").resolve()),
                    FileSystemLoader(Path(f"{Path(__file__).parent.resolve()}/templates").resolve()),
                ]
            ),
            lstrip_blocks=True,
            trim_blocks=True,
        )
        env.filters["randomsuffix"] = self._filter_randomsuffix
        env.filters["totf"] = self._filter_totf

        try:
            content = env.get_template(name="main.tf.jinja").render(**data)
        except TemplateNotFound as e:
            raise Exception(f"Template not found '{e.message}'") from e  # noqa: TRY002

        self._save_to_file("main.tf", content)

    def _load_data(self) -> dict:
        path = Path(f"{__file__}/../../tmp/data.json").resolve()

        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)

    def _save_to_file(self, filename: str, content: str):
        folder = Path(f"{__file__}/../../tmp/code").resolve()
        if not Path.exists(folder):
            Path.mkdir(folder, parents=True)

        with Path(f"{folder}/{filename}").open("w", encoding="utf-8") as fp:
            fp.write(content)

    def _validate_code(self) -> None:
        if not is_command_available("terraform"):
            logging.warning("Terraform is not installed. Skipping generated Terraform code validation.")
            return

        code_folder_path = Path(f"{__file__}/../../tmp/code").resolve()
        process = subprocess.run(
            f"terraform -chdir={code_folder_path} init -backend=false -no-color && terraform -chdir={code_folder_path} validate -no-color",  # noqa: E501
            capture_output=True,
            check=False,
            shell=True,
            text=True,
        )

        if process.returncode != 0:
            logging.warning(f"Generated Terraform code is invalid: {process.stderr}")
        else:
            logging.info("Generated Terraform code is valid")

    def generate(self):
        """Generate source code for managing Spacelift entities"""
        data = self._load_data()
        self._generate_code(data)
        self._format_code()
        self._validate_code()
