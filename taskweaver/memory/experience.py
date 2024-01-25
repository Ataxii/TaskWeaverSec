import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np
from injector import inject
from sklearn.metrics.pairwise import cosine_similarity

from taskweaver.config.module_config import ModuleConfig
from taskweaver.llm import LLMApi, format_chat_message
from taskweaver.logging import TelemetryLogger
from taskweaver.utils import read_yaml, write_yaml


@dataclass
class Experience:
    experience_text: str
    exp_id: str
    raw_experience_path: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding: List[float] = field(default_factory=list)

    def to_dict(self):
        return {
            "exp_id": self.exp_id,
            "experience_text": self.experience_text,
            "raw_experience_path": self.raw_experience_path,
            "embedding_model": self.embedding_model,
            "embedding": self.embedding,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]):
        return Experience(
            exp_id=d["exp_id"],
            experience_text=d["experience_text"],
            raw_experience_path=d["raw_experience_path"] if "raw_experience_path" in d else None,
            embedding_model=d["embedding_model"] if "embedding_model" in d else None,
            embedding=d["embedding"] if "embedding" in d else [],
        )


class ExperienceConfig(ModuleConfig):
    def _configure(self) -> None:
        self._set_name("experience")

        self.experience_dir = self._get_path(
            "experience_dir",
            os.path.join(self.src.app_base_path, "experience"),
        )
        self.default_exp_prompt_path = self._get_path(
            "default_exp_prompt_path",
            os.path.join(
                os.path.dirname(__file__),
                "default_exp_prompt.yaml",
            ),
        )
        self.retrieve_threshold = self._get_float("retrieve_threshold", 0.2)


class ExperienceGenerator:
    @inject
    def __init__(
        self,
        llm_api: LLMApi,
        config: ExperienceConfig,
        logger: TelemetryLogger,
    ):
        self.config = config
        self.llm_api = llm_api
        self.logger = logger

        self.default_prompt_template = read_yaml(self.config.default_exp_prompt_path)["content"]

        self.experience_list: List[Experience] = []

        self.exception_message_for_refresh = (
            "Please cd to the `script` directory and "
            "run `python -m experience_mgt --refresh` to refresh the experience."
        )

    @staticmethod
    def _preprocess_conversation_data(
        conv_data: dict,
        target_role: Literal["Planner", "CodeInterpreter", "All"],
    ):
        def remove_id_fields(d):
            if isinstance(d, dict):
                for key in list(d.keys()):
                    if key == "id":
                        del d[key]
                    else:
                        remove_id_fields(d[key])
            elif isinstance(d, list):
                for item in d:
                    remove_id_fields(item)

        conv_data = conv_data["rounds"]
        remove_id_fields(conv_data)

        return conv_data

    def summarize_experience(
        self,
        exp_id: str,
        prompt: Optional[str] = None,
        target_role: Literal["Planner", "CodeInterpreter", "All"] = "All",
    ):
        raw_exp_file_path = os.path.join(self.config.experience_dir, f"raw_exp_{exp_id}.yaml")
        conversation = read_yaml(raw_exp_file_path)

        conversation = self._preprocess_conversation_data(conversation, target_role)

        system_instruction = prompt if prompt else self.default_prompt_template
        prompt = [
            format_chat_message("system", system_instruction),
            format_chat_message("user", json.dumps(conversation)),
        ]
        summarized_experience = self.llm_api.chat_completion(prompt)["content"]

        return summarized_experience

    def refresh(
        self,
        target_role: Literal["Planner", "CodeInterpreter", "All"],
        prompt: Optional[str] = None,
    ):
        if not os.path.exists(self.config.experience_dir):
            raise ValueError(f"Experience directory {self.config.experience_dir} does not exist.")

        exp_files = os.listdir(self.config.experience_dir)

        raw_exp_ids = [
            os.path.splitext(os.path.basename(exp_file))[0].split("_")[2]
            for exp_file in exp_files
            if exp_file.startswith("raw_exp")
        ]

        handcrafted_exp_ids = [
            os.path.splitext(os.path.basename(exp_file))[0].split("_")[2]
            for exp_file in exp_files
            if exp_file.startswith("handcrafted_exp")
        ]

        exp_ids = raw_exp_ids + handcrafted_exp_ids

        if len(exp_ids) == 0:
            self.logger.warning(
                "No raw experience found. "
                "Please type /save in the chat window to save raw experience"
                "or write handcrafted experience.",
            )
            return

        to_be_embedded = []
        for idx, exp_id in enumerate(exp_ids):
            rebuild_flag = False
            exp_file_name = f"{target_role}_exp_{exp_id}.yaml"
            if exp_file_name not in os.listdir(self.config.experience_dir):
                rebuild_flag = True
            else:
                exp_file_path = os.path.join(self.config.experience_dir, exp_file_name)
                experience = read_yaml(exp_file_path)
                if (
                    experience["embedding_model"] != self.llm_api.embedding_service.config.embedding_model
                    or len(experience["embedding"]) == 0
                ):
                    rebuild_flag = True

            if rebuild_flag:
                if exp_id in raw_exp_ids:
                    summarized_experience = self.summarize_experience(exp_id, prompt, target_role)
                    experience_obj = Experience(
                        experience_text=summarized_experience,
                        exp_id=exp_id,
                        raw_experience_path=os.path.join(
                            self.config.experience_dir,
                            f"raw_exp_{exp_id}.yaml",
                        ),
                    )
                elif exp_id in handcrafted_exp_ids:
                    handcrafted_exp_file_path = os.path.join(
                        self.config.experience_dir,
                        f"handcrafted_exp_{exp_id}.yaml",
                    )
                    experience_obj = Experience.from_dict(read_yaml(handcrafted_exp_file_path))
                else:
                    raise ValueError(f"Experience {exp_id} not found in raw or handcrafted experience.")

                to_be_embedded.append(experience_obj)

        if len(to_be_embedded) == 0:
            return
        else:
            exp_embeddings = self.llm_api.get_embedding_list(
                [exp.experience_text for exp in to_be_embedded],
            )
            for i, exp in enumerate(to_be_embedded):
                exp.embedding = exp_embeddings[i]
                exp.embedding_model = self.llm_api.embedding_service.config.embedding_model
                experience_file_path = os.path.join(self.config.experience_dir, f"{target_role}_exp_{exp.exp_id}.yaml")
                write_yaml(experience_file_path, exp.to_dict())

            self.logger.info("Experience obj saved.")

    def load_experience(
        self,
        target_role: Literal["Planner", "CodeInterpreter", "All"],
    ):
        if not os.path.exists(self.config.experience_dir):
            raise ValueError(f"Experience directory {self.config.experience_dir} does not exist.")

        original_exp_files = [
            exp_file
            for exp_file in os.listdir(self.config.experience_dir)
            if exp_file.startswith("raw_exp_") or exp_file.startswith("handcrafted_exp_")
        ]
        exp_ids = [os.path.splitext(os.path.basename(exp_file))[0].split("_")[2] for exp_file in original_exp_files]
        if len(exp_ids) == 0:
            self.logger.warning(
                f"No experience found for {target_role}."
                f"Please type /save in the chat window to save raw experience or write handcrafted experience."
                + self.exception_message_for_refresh,
            )
            return

        for exp_id in exp_ids:
            exp_file = f"{target_role}_exp_{exp_id}.yaml"
            exp_file_path = os.path.join(self.config.experience_dir, exp_file)
            assert os.path.exists(exp_file_path), (
                f"Experience {exp_file} for {target_role} not found. " + self.exception_message_for_refresh
            )

            experience = read_yaml(exp_file_path)

            assert len(experience["embedding"]) > 0, (
                f"Experience {exp_file} has no embedding." + self.exception_message_for_refresh
            )
            assert experience["embedding_model"] == self.llm_api.embedding_service.config.embedding_model, (
                f"Experience {exp_file} has different embedding model. " + self.exception_message_for_refresh
            )

            self.experience_list.append(Experience(**experience))

    def retrieve_experience(self, user_query: str) -> List[Tuple[Experience, float]]:
        user_query_embedding = np.array(self.llm_api.get_embedding(user_query))

        similarities = []

        for experience in self.experience_list:
            similarity = cosine_similarity(
                user_query_embedding.reshape(
                    1,
                    -1,
                ),
                np.array(experience.embedding).reshape(1, -1),
            )
            similarities.append((experience, similarity))

        experience_rank = sorted(
            similarities,
            key=lambda x: x[1],
            reverse=True,
        )

        selected_experiences = [(exp, sim) for exp, sim in experience_rank if sim >= self.config.retrieve_threshold]
        self.logger.info(f"Retrieved {len(selected_experiences)} experiences.")
        self.logger.info(f"Retrieved experiences: {[exp.exp_id for exp, sim in selected_experiences]}")
        return selected_experiences

    def _delete_exp_file(self, exp_file_name: str):
        if exp_file_name in os.listdir(self.config.experience_dir):
            os.remove(os.path.join(self.config.experience_dir, exp_file_name))
            self.logger.info(f"Experience {exp_file_name} deleted.")
        else:
            self.logger.info(f"Experience {exp_file_name} not found.")

    def delete_experience(self, exp_id: str, target_role: Literal["Planner", "CodeInterpreter"]):
        exp_file_name = f"{target_role}_exp_{exp_id}.yaml"
        self._delete_exp_file(exp_file_name)

    def delete_raw_experience(self, exp_id: str):
        exp_file_name = f"raw_exp_{exp_id}.yaml"
        self._delete_exp_file(exp_file_name)

    def delete_handcrafted_experience(self, exp_id: str):
        exp_file_name = f"handcrafted_exp_{exp_id}.yaml"
        self._delete_exp_file(exp_file_name)

    @staticmethod
    def format_experience_in_prompt(
        prompt_template: str,
        selected_experiences: Optional[List[Experience]] = None,
    ):
        if selected_experiences is not None and len(selected_experiences) > 0:
            return prompt_template.format(
                experiences="===================\n"
                + "\n===================\n".join(
                    [exp.experience_text for exp, sim in selected_experiences],
                ),
            )
        else:
            return ""
