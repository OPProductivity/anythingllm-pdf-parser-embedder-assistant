"""Immutable operator intent for a future incremental preparation migration.

This module is intentionally not wired into the active Gradio or CLI paths
yet.  It gives those callers a tested compatibility boundary without changing
the legacy engine's behavior, cleanup policy, or transport decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Mapping


LOCAL_ONLY = "local_only"
NATIVE_UPLOAD = "native_upload"
SEGMENT_MODES = frozenset({"none", "passages", "page", "page_limit"})
BACKEND_MODES = frozenset({"automatic", "pymupdf", "pymupdf4llm", "unstructured"})
UNSTRUCTURED_STRATEGIES = frozenset({"auto", "fast", "hi_res", "ocr_only"})
MARKER_STYLES = frozenset({"short", "compact", "full"})
UPLOAD_TRANSPORTS = frozenset({"raw_text", "file_upload"})
UPLOAD_REPRESENTATIONS = frozenset({"segments", "page_parents"})
METADATA_MODES = frozenset({"native_header", "strict"})
TEMPORARY_CLEANUP_POLICIES = frozenset({"cleanup_always", "cleanup_on_success", "retain_for_review"})


def _paths(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (str(value),) if str(value).strip() else ()
    return tuple(str(item) for item in value if str(item).strip())


def _lines(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    return tuple(str(item).strip() for item in value if str(item).strip())


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Validated operator intent, deliberately separated from runtime facts."""

    input_paths: tuple[str, ...]
    mode: str = LOCAL_ONLY
    document_label: str = ""
    document_author: str = ""
    document_short_label: str = ""
    use_file_title_fallback: bool = False
    output_root_override: str | None = None
    retain_diagnostic_evidence: bool = False

    backend_mode: str = "automatic"
    deep_extraction: bool = False
    include_front_matter: bool = False
    include_back_matter: bool = False
    first_page_override: int | None = None
    end_page_override: int | None = None
    end_section_names: tuple[str, ...] = ()
    validation_phrases: tuple[str, ...] = ()
    unstructured_strategy: str = "auto"
    marker_style: str = "short"
    inline_markers_enabled: bool = True

    segment_mode: str = "page_limit"
    target_passage_length: int = 750
    anythingllm_chunk_size_override: int | None = None
    anythingllm_chunk_overlap_override: int | None = None
    run_vector_evaluation: bool = False
    max_vector_probes: int = 8
    max_vector_chunks: int = 300
    ollama_model: str = "bge-m3:latest"
    ollama_url: str = "http://127.0.0.1:11434/api/embed"

    api_url: str | None = None
    workspace_slug: str | None = None
    upload_limit: int = 0
    native_metadata_upload_mode: str = "native_header"
    native_upload_representation: str = "segments"
    native_upload_transport: str = "raw_text"
    # The default mode is local-only, where native document folders are
    # invalid intent. Native adapters explicitly opt into their established
    # default of ``True`` when they construct an upload request.
    create_document_folders: bool = False
    document_folder_name: str | None = None
    temporary_validation_requested: bool = False
    temporary_validation_cleanup_policy: str = "cleanup_always"

    def __post_init__(self) -> None:
        if not self.input_paths:
            raise ValueError("RunRequest requires at least one input path.")
        if self.mode not in {LOCAL_ONLY, NATIVE_UPLOAD}:
            raise ValueError(f"Unsupported run mode: {self.mode!r}")
        if self.backend_mode not in BACKEND_MODES:
            raise ValueError(f"Unsupported backend mode: {self.backend_mode!r}")
        if self.segment_mode not in SEGMENT_MODES:
            raise ValueError(f"Unsupported segment mode: {self.segment_mode!r}")
        if self.unstructured_strategy not in UNSTRUCTURED_STRATEGIES:
            raise ValueError(f"Unsupported Unstructured strategy: {self.unstructured_strategy!r}")
        if self.marker_style not in MARKER_STYLES:
            raise ValueError(f"Unsupported marker style: {self.marker_style!r}")
        if self.target_passage_length < 100:
            raise ValueError("target_passage_length must be at least 100 characters.")
        if self.anythingllm_chunk_size_override is not None and self.anythingllm_chunk_size_override < 1:
            raise ValueError("anythingllm_chunk_size_override must be positive when specified.")
        if self.anythingllm_chunk_overlap_override is not None and self.anythingllm_chunk_overlap_override < 0:
            raise ValueError("anythingllm_chunk_overlap_override cannot be negative.")
        if (
            self.anythingllm_chunk_size_override is not None
            and self.anythingllm_chunk_overlap_override is not None
            and self.anythingllm_chunk_overlap_override >= self.anythingllm_chunk_size_override
        ):
            raise ValueError("anythingllm chunk overlap must be smaller than chunk size.")
        if self.first_page_override is not None and self.first_page_override < 1:
            raise ValueError("first_page_override must be positive when specified.")
        if self.end_page_override is not None and self.end_page_override < 1:
            raise ValueError("end_page_override must be positive when specified.")
        if (
            self.first_page_override is not None
            and self.end_page_override is not None
            and self.end_page_override < self.first_page_override
        ):
            raise ValueError("end_page_override cannot precede first_page_override.")
        if self.upload_limit < 0:
            raise ValueError("upload_limit cannot be negative.")
        if self.native_metadata_upload_mode not in METADATA_MODES:
            raise ValueError("Unsupported native metadata upload mode.")
        if self.native_upload_representation not in UPLOAD_REPRESENTATIONS:
            raise ValueError("Unsupported native upload representation.")
        if self.native_upload_transport not in UPLOAD_TRANSPORTS:
            raise ValueError("Unsupported native upload transport.")
        if self.temporary_validation_cleanup_policy not in TEMPORARY_CLEANUP_POLICIES:
            raise ValueError("Unsupported temporary validation cleanup policy.")
        if self.mode == LOCAL_ONLY and any((
            self.api_url, self.workspace_slug, self.create_document_folders, self.document_folder_name,
        )):
            raise ValueError("A local-only request cannot carry native-upload mutation intent.")

    @classmethod
    def from_automatic_settings(
        cls,
        settings: Mapping[str, Any],
        *,
        mode: str,
        segment_mode: str,
        native_metadata_upload_mode: str = "native_header",
        native_upload_transport: str = "raw_text",
        upload_limit: int = 0,
    ) -> "RunRequest":
        """Adapt name-keyed Gradio settings after the UI resolves its labels.

        The caller passes normalized values for UI labels (mode, segment mode,
        metadata mode, transport, and upload limit). This keeps Gradio display
        strings out of the shared request contract.
        """
        # The live confirmation flow has already merged direct picker and
        # folder-picker sources into ``files``. Retain the two older keys for
        # direct callers and historical settings snapshots, but prefer the
        # normalized list so this typed boundary can be enabled without
        # rejecting every current multi-file confirmation as input-less.
        inputs = _paths(settings.get("files")) or (
            _paths(settings.get("pdf_files")) + _paths(settings.get("folder_pdf_files"))
        )
        native = mode == NATIVE_UPLOAD
        inherit = bool(settings.get("inherit_anythingllm_settings"))
        return cls(
            input_paths=inputs,
            mode=mode,
            document_label=str(settings.get("document_label") or "").strip(),
            document_author=str(settings.get("document_author") or "").strip(),
            document_short_label=str(settings.get("document_short_label") or "").strip(),
            use_file_title_fallback=bool(settings.get("use_file_title_fallback")),
            output_root_override=_optional_text(settings.get("output_root_override")),
            retain_diagnostic_evidence=not bool(settings.get("lean_retention", True)),
            backend_mode=str(settings.get("backend_mode") or "automatic").casefold(),
            deep_extraction=bool(settings.get("deep_extraction")),
            include_front_matter=bool(settings.get("include_front_matter")),
            include_back_matter=bool(settings.get("include_back_matter")),
            first_page_override=int(settings.get("first_page_override") or 0) or None,
            end_page_override=int(settings.get("end_page_override") or 0) or None,
            end_section_names=_lines(settings.get("advanced_end_section_names")),
            validation_phrases=_lines(settings.get("automatic_validation_phrases")),
            unstructured_strategy=str(settings.get("unstructured_strategy") or "auto").casefold(),
            segment_mode=segment_mode,
            target_passage_length=int(settings.get("target_passage_length") or 750),
            anythingllm_chunk_size_override=None if inherit else int(settings.get("anythingllm_chunk_size") or 0) or None,
            anythingllm_chunk_overlap_override=None if inherit else max(0, int(settings.get("anythingllm_chunk_overlap") or 0)),
            run_vector_evaluation=bool(settings.get("run_vector_eval")),
            ollama_model=str(settings.get("custom_ollama_model") or "bge-m3:latest").strip() or "bge-m3:latest",
            ollama_url=str(settings.get("ollama_url") or "http://127.0.0.1:11434/api/embed").strip(),
            api_url=_optional_text(settings.get("api_url")) if native else None,
            workspace_slug=_optional_text(settings.get("workspace_slug")) if native else None,
            upload_limit=upload_limit if native else 0,
            native_metadata_upload_mode=native_metadata_upload_mode,
            native_upload_transport=native_upload_transport,
            create_document_folders=bool(settings.get("anythingllm_create_document_folders", True)) if native else False,
            document_folder_name=_optional_text(settings.get("anythingllm_document_folder_name")) if native else None,
        )

    @classmethod
    def from_cli_namespace(cls, args: Any) -> "RunRequest":
        """Adapt current argparse output without retaining its API key."""
        native = bool(getattr(args, "prepare_and_upload", False))
        raw_overlap = getattr(args, "anythingllm_chunk_overlap", -1)
        return cls(
            input_paths=_paths(getattr(args, "input", "")),
            mode=NATIVE_UPLOAD if native else LOCAL_ONLY,
            document_label=str(getattr(args, "document_label", "") or "").strip(),
            document_author=str(getattr(args, "document_author", "") or "").strip(),
            document_short_label=str(getattr(args, "document_short_label", "") or "").strip(),
            output_root_override=_optional_text(getattr(args, "out_dir", "")),
            retain_diagnostic_evidence=not bool(getattr(args, "lean_retention", True)),
            backend_mode=str(getattr(args, "backend_mode", "automatic") or "automatic").casefold(),
            deep_extraction=bool(getattr(args, "deep_extraction", False)),
            include_front_matter=bool(getattr(args, "include_front_matter", False)),
            first_page_override=int(getattr(args, "first_page_override", 0) or 0) or None,
            end_page_override=int(getattr(args, "end_page_override", 0) or 0) or None,
            end_section_names=_lines(getattr(args, "end_section_names", ())),
            validation_phrases=_lines(getattr(args, "validation_phrases", ())),
            unstructured_strategy=str(getattr(args, "unstructured_strategy", "auto") or "auto").casefold(),
            marker_style=str(getattr(args, "marker_style", "short") or "short").casefold(),
            inline_markers_enabled=not bool(getattr(args, "disable_inline_markers", False)),
            segment_mode=str(getattr(args, "segment_mode", "page_limit") or "page_limit"),
            target_passage_length=int(getattr(args, "target_passage_length", 750) or 750),
            anythingllm_chunk_size_override=int(getattr(args, "anythingllm_chunk_size", 0) or 0) or None,
            anythingllm_chunk_overlap_override=(
                None if raw_overlap is None or int(raw_overlap) < 0 else int(raw_overlap)
            ),
            run_vector_evaluation=bool(getattr(args, "run_vector_eval", False)),
            ollama_model=str(getattr(args, "ollama_model", "bge-m3:latest") or "bge-m3:latest"),
            ollama_url=str(getattr(args, "ollama_url", "http://127.0.0.1:11434/api/embed") or ""),
            api_url=_optional_text(getattr(args, "anythingllm_api_url", "")) if native else None,
            workspace_slug=_optional_text(getattr(args, "workspace_slug", "")) if native else None,
            upload_limit=int(getattr(args, "upload_limit", 0) or 0) if native else 0,
            native_metadata_upload_mode=str(getattr(args, "native_metadata_upload_mode", "native_header") or "native_header"),
            native_upload_representation=str(getattr(args, "native_upload_representation", "segments") or "segments"),
            native_upload_transport=str(getattr(args, "native_upload_transport", "raw_text") or "raw_text"),
            create_document_folders=bool(getattr(args, "anythingllm_create_document_folders", True)) if native else False,
            temporary_validation_requested=bool(getattr(args, "run_chunk_survival_validation", False)),
            temporary_validation_cleanup_policy=str(
                getattr(args, "temporary_validation_cleanup_policy", "cleanup_always") or "cleanup_always"
            ),
        )

    def to_legacy_namespace(
        self,
        *,
        resolved_api_key: str = "",
        simulation_adapter: Mapping[str, Any] | None = None,
        callbacks: Mapping[str, Callable[..., Any] | None] | None = None,
    ) -> SimpleNamespace:
        """Project intent plus injected runtime dependencies to the legacy engine.

        Secrets and callbacks are injected at execution time and never become
        fields on the immutable request itself.
        """
        callbacks = callbacks or {}
        return SimpleNamespace(
            document_label=self.document_label,
            document_author=self.document_author,
            document_short_label=self.document_short_label,
            use_file_title_fallback=self.use_file_title_fallback,
            deep_extraction=self.deep_extraction,
            include_front_matter=self.include_front_matter,
            include_back_matter=self.include_back_matter,
            backend_mode=self.backend_mode,
            first_page_override=self.first_page_override or 0,
            end_page_override=self.end_page_override or 0,
            target_passage_length=self.target_passage_length,
            segment_mode=self.segment_mode,
            end_section_names=list(self.end_section_names),
            validation_phrases=list(self.validation_phrases),
            unstructured_strategy=self.unstructured_strategy,
            anythingllm_chunk_size=self.anythingllm_chunk_size_override or 0,
            anythingllm_chunk_overlap=-1 if self.anythingllm_chunk_overlap_override is None else self.anythingllm_chunk_overlap_override,
            marker_style=self.marker_style,
            disable_inline_markers=not self.inline_markers_enabled,
            lean_retention=not self.retain_diagnostic_evidence,
            run_vector_eval=self.run_vector_evaluation,
            ollama_model=self.ollama_model,
            ollama_url=self.ollama_url,
            max_vector_probes=self.max_vector_probes,
            max_vector_chunks=self.max_vector_chunks,
            prepare_and_upload=self.mode == NATIVE_UPLOAD,
            anythingllm_api_url=self.api_url or "",
            anythingllm_api_key=resolved_api_key,
            workspace_slug=self.workspace_slug or "",
            test_workspace_slug=self.workspace_slug or "test",
            upload_limit=self.upload_limit,
            native_metadata_upload_mode=self.native_metadata_upload_mode,
            native_upload_representation=self.native_upload_representation,
            native_upload_transport=self.native_upload_transport,
            anythingllm_create_document_folders=self.create_document_folders,
            anythingllm_document_folder_name=self.document_folder_name or "",
            anythingllm_storage_dir="",
            run_chunk_survival_validation=self.temporary_validation_requested,
            temporary_validation_cleanup_policy=self.temporary_validation_cleanup_policy,
            simulation_adapter=simulation_adapter,
            progress_callback=callbacks.get("progress_callback"),
            timing_event_callback=callbacks.get("timing_event_callback"),
            cancel_callback=callbacks.get("cancel_callback"),
        )
