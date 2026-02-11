"""spaCy 3.7.x NLP-based entity recognition detector for PII identification.

This module implements the NLP layer of the dual-layer PII detection approach
used by the Compliance Service. It leverages spaCy's pre-trained named entity
recognition (NER) models to detect unstructured PII that regex patterns cannot
reliably identify, including person names, geographic locations, addresses,
and organization names.

Key Features:
    - Named entity recognition using spaCy's ``en_core_web_sm`` model
    - Detection of PERSON names, GPE/LOC addresses, and ORG organizations
    - Heuristic confidence scoring based on contextual clues
    - Efficient batch processing via spaCy's ``nlp.pipe()``
    - Air-gapped readiness: model loaded from local installation only
    - Structured JSON logging for model loading and detection events

The NLPDetector works alongside the ``PatternDetector`` (regex-based) to
provide comprehensive PII coverage.  While the PatternDetector handles
structured patterns such as SSNs, emails, and phone numbers, this NLPDetector
identifies free-form PII requiring natural-language understanding.

Usage::

    from compliance_service.detectors.nlp_detector import NLPDetector

    detector = NLPDetector()
    result = detector.detect("John Smith lives in New York and works at Acme Corp.")
    print(result.has_pii)       # True
    print(result.entity_count)  # 3

    # Batch processing for dataset columns
    results = detector.detect_batch([
        "Jane Doe visited Paris.",
        "No PII in this text.",
    ])
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import spacy
from pydantic import BaseModel, Field

from shared.logging.structured_logger import get_logger


if TYPE_CHECKING:
    from spacy.tokens import Doc, Span


# ---------------------------------------------------------------------------
# Pydantic data models for NLP detection results
# ---------------------------------------------------------------------------


class NLPEntity(BaseModel):
    """A single named entity detected by the spaCy NER pipeline.

    Represents one detected entity from the NLP analysis, including its
    text content, spaCy label, character offsets, heuristic confidence
    score, and mapped PII category type.

    Attributes:
        text: The surface form of the detected entity (e.g., ``"John Smith"``).
        label: The spaCy NER label (e.g., ``"PERSON"``, ``"GPE"``, ``"ORG"``).
        start_char: Zero-based start character offset in the source text.
        end_char: Zero-based end character offset (exclusive) in source text.
        confidence: Detection confidence score in the range ``[0.0, 1.0]``.
        pii_type: Mapped PII category (e.g., ``"PERSON_NAME"``, ``"ADDRESS"``).

    Example::

        entity = NLPEntity(
            text="John Smith",
            label="PERSON",
            start_char=0,
            end_char=10,
            confidence=0.95,
            pii_type="PERSON_NAME",
        )
    """

    text: str = Field(
        ...,
        description="The detected entity text",
    )
    label: str = Field(
        ...,
        description="spaCy entity label (PERSON, GPE, LOC, ORG, DATE, NORP)",
    )
    start_char: int = Field(
        ...,
        ge=0,
        description="Start character offset in source text",
    )
    end_char: int = Field(
        ...,
        ge=0,
        description="End character offset in source text (exclusive)",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Detection confidence score [0.0, 1.0]",
    )
    pii_type: str = Field(
        ...,
        description="Mapped PII category (e.g., PERSON_NAME, ADDRESS, ORGANIZATION)",
    )


class NLPDetectionResult(BaseModel):
    """Aggregate result of NLP-based PII detection on a single text input.

    Contains all detected entities, summary statistics, and performance
    metrics from processing a text through the spaCy NER pipeline.

    Attributes:
        entities: List of all detected named entities with PII relevance.
        has_pii: ``True`` if at least one PII-relevant entity was detected.
        entity_count: Total number of PII entities detected.
        processing_time_ms: Wall-clock time in the spaCy pipeline (milliseconds).
        model_name: The spaCy model used for detection.

    Example::

        result = NLPDetectionResult(
            entities=[...],
            has_pii=True,
            entity_count=3,
            processing_time_ms=12.5,
            model_name="en_core_web_sm",
        )
    """

    entities: list[NLPEntity] = Field(
        default_factory=list,
        description="All detected entities",
    )
    has_pii: bool = Field(
        default=False,
        description="Whether any PII entities were found",
    )
    entity_count: int = Field(
        default=0,
        ge=0,
        description="Total number of entities detected",
    )
    processing_time_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="spaCy processing time in milliseconds",
    )
    model_name: str = Field(
        default="en_core_web_sm",
        description="The spaCy model used",
    )


# ---------------------------------------------------------------------------
# Internal data structures (lightweight, no Pydantic validation overhead)
# ---------------------------------------------------------------------------


@dataclass
class _DetectionContext:
    """Internal lightweight context for tracking detection state.

    Used inside ``NLPDetector`` to aggregate intermediate state during
    entity extraction without incurring Pydantic validation overhead on
    every processing step.

    Attributes:
        text_length: Length of the input text in characters.
        entity_positions: Start/end character pairs for detected entities.
        honorific_positions: Token indices where honorifics were found.
        preposition_positions: Token indices where address prepositions were found.
    """

    text_length: int = 0
    entity_positions: list[tuple[int, int]] = field(default_factory=list)
    honorific_positions: list[int] = field(default_factory=list)
    preposition_positions: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Context-pattern lookup tables for confidence boosting heuristics.
# Defined at module level as frozen sets for O(1) membership checks
# and immutability guarantees.
# ---------------------------------------------------------------------------

_HONORIFICS: frozenset[str] = frozenset({
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "dr", "dr.",
    "prof", "prof.", "sir", "dame", "lord", "lady", "rev", "rev.",
})
"""Honorific tokens that precede person names and boost PERSON confidence."""

_ADDRESS_PREPOSITIONS: frozenset[str] = frozenset({
    "at", "in", "from", "near", "to", "of", "on",
})
"""Prepositions that indicate address/location context for GPE/LOC entities."""

_CORPORATE_SUFFIXES: frozenset[str] = frozenset({
    "inc", "inc.", "corp", "corp.", "ltd", "ltd.", "llc", "llp",
    "gmbh", "co", "co.", "plc", "ag", "sa",
})
"""Corporate suffix tokens that boost ORG entity confidence."""


# ---------------------------------------------------------------------------
# Main NLP Detector
# ---------------------------------------------------------------------------


class NLPDetector:
    """spaCy-powered NLP entity detector for identifying unstructured PII.

    This detector provides the NLP layer of the Compliance Service's dual-layer
    PII detection approach.  While the companion ``PatternDetector`` handles
    structured patterns (SSNs, emails, phone numbers), the ``NLPDetector``
    identifies unstructured PII that requires natural-language understanding:
    person names, geographic locations, addresses, and organization names.

    The detector loads a pre-trained spaCy model (``en_core_web_sm`` by default)
    at initialization and processes text through the full NLP pipeline to extract
    named entities.  Each entity is assigned a heuristic confidence score based
    on contextual clues such as honorifics, prepositions, and corporate suffixes.

    Attributes:
        ENTITY_TYPE_MAP: Mapping from spaCy NER labels to PII type categories.
        PII_RELEVANT_ENTITIES: Set of entity labels considered PII-relevant.

    Args:
        model_name: The spaCy model to load.  Defaults to ``"en_core_web_sm"``.
            Must be pre-installed locally for air-gapped operation.
        pii_entity_types: Optional list of spaCy entity labels to treat as PII.
            Defaults to ``["PERSON", "GPE", "LOC", "ORG"]``.
        confidence_threshold: Minimum confidence score for an entity to be
            included in results.  Defaults to ``0.85``.

    Raises:
        OSError: If the specified spaCy model is not installed locally.

    Example::

        detector = NLPDetector()
        result = detector.detect("John Smith lives in New York.")
        for entity in result.entities:
            print(f"{entity.text} -> {entity.pii_type} ({entity.confidence:.2f})")
        # John Smith -> PERSON_NAME (0.95)
        # New York -> ADDRESS (0.90)
    """

    ENTITY_TYPE_MAP: dict[str, str] = {
        "PERSON": "PERSON_NAME",
        "GPE": "ADDRESS",
        "LOC": "ADDRESS",
        "ORG": "ORGANIZATION",
        "DATE": "DATE",
        "NORP": "DEMOGRAPHIC",
    }
    """Maps spaCy NER labels to standardised PII type categories."""

    PII_RELEVANT_ENTITIES: set[str] = {"PERSON", "GPE", "LOC", "ORG"}
    """Entity labels that are classified as PII-relevant by default."""

    def __init__(
        self,
        model_name: str = "en_core_web_sm",
        pii_entity_types: list[str] | None = None,
        confidence_threshold: float = 0.85,
    ) -> None:
        """Initialize the NLP detector by loading the spaCy model.

        Loads the specified spaCy model from the local installation (no
        internet access required, supporting air-gapped deployment).
        Configures the PII entity type filter and confidence threshold
        for all subsequent detection operations.

        Args:
            model_name: spaCy model name to load.  Must be pre-installed
                via ``python -m spacy download en_core_web_sm`` or bundled
                in the Docker image for air-gapped environments.
            pii_entity_types: Optional override for which spaCy entity
                labels are considered PII.  When ``None``, uses the
                class-level ``PII_RELEVANT_ENTITIES`` set.
            confidence_threshold: Minimum heuristic confidence score for
                entity inclusion in results.  Entities scoring below this
                threshold are silently excluded.

        Raises:
            OSError: If the spaCy model cannot be found locally.  The
                error message includes installation instructions.

        Example::

            # Default configuration
            detector = NLPDetector()

            # Custom configuration for stricter detection
            detector = NLPDetector(
                model_name="en_core_web_lg",
                pii_entity_types=["PERSON", "ORG"],
                confidence_threshold=0.90,
            )
        """
        self._logger = get_logger(__name__)
        self._model_name: str = model_name
        self._confidence_threshold: float = confidence_threshold

        # Override PII-relevant entity types when caller provides a custom list
        if pii_entity_types is not None:
            self.PII_RELEVANT_ENTITIES = set(pii_entity_types)

        # Load spaCy model from local installation (air-gapped safe).
        # spacy.load() resolves the model from the local Python environment;
        # no network call is made.
        try:
            self._nlp: Any = spacy.load(model_name)
        except OSError as exc:
            self._logger.error(
                "spacy_model_not_found",
                model_name=model_name,
                error=str(exc),
                instructions=(
                    f"Install the model with: python -m spacy download {model_name} "
                    f"or ensure it is pre-downloaded in the Docker image for "
                    f"air-gapped deployment."
                ),
            )
            raise OSError(
                f"spaCy model '{model_name}' not found. Install it with: "
                f"python -m spacy download {model_name} — "
                f"For air-gapped deployment, ensure the model is bundled in "
                f"the Docker image."
            ) from exc

        # Log successful model load with pipeline component info
        pipeline_components: list[str] = [
            name for name, _component in self._nlp.pipeline
        ]
        self._logger.info(
            "spacy_model_loaded",
            model_name=model_name,
            pipeline_components=pipeline_components,
            pii_entity_types=sorted(self.PII_RELEVANT_ENTITIES),
            confidence_threshold=confidence_threshold,
        )

    # ------------------------------------------------------------------
    # Public detection API
    # ------------------------------------------------------------------

    def detect(self, text: str) -> NLPDetectionResult:
        """Detect named entities in a single text input via spaCy NER.

        Processes the input text through the full spaCy NLP pipeline,
        extracts named entities, filters to PII-relevant types, calculates
        heuristic confidence scores, and returns a structured result object.

        Args:
            text: The input text to scan for named entities.  May be empty,
                whitespace-only, or contain multiple sentences.

        Returns:
            An ``NLPDetectionResult`` containing all detected PII entities,
            a summary flag, entity count, and performance timing.

        Example::

            detector = NLPDetector()
            result = detector.detect(
                "Dr. Jane Smith from Microsoft Corp. visited London."
            )
            assert result.has_pii is True
            assert result.entity_count >= 2
            print(f"Processing took {result.processing_time_ms:.1f}ms")
        """
        start_time: float = time.perf_counter()

        # Fast-path for empty or whitespace-only input
        if not text or not text.strip():
            elapsed_ms: float = (time.perf_counter() - start_time) * 1000.0
            return NLPDetectionResult(
                entities=[],
                has_pii=False,
                entity_count=0,
                processing_time_ms=round(elapsed_ms, 2),
                model_name=self._model_name,
            )

        # Run the full spaCy pipeline (tokenizer → NER → …)
        doc: Doc = self._nlp(text)

        # Build lightweight detection context for confidence scoring
        context: _DetectionContext = self._build_detection_context(doc)

        # Extract, filter, and score named entities
        detected_entities: list[NLPEntity] = self._extract_entities(doc, context)

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        has_pii: bool = len(detected_entities) > 0

        self._logger.debug(
            "nlp_detection_complete",
            text_length=context.text_length,
            entity_count=len(detected_entities),
            has_pii=has_pii,
            processing_time_ms=round(elapsed_ms, 2),
            model_name=self._model_name,
        )

        return NLPDetectionResult(
            entities=detected_entities,
            has_pii=has_pii,
            entity_count=len(detected_entities),
            processing_time_ms=round(elapsed_ms, 2),
            model_name=self._model_name,
        )

    def detect_batch(self, texts: list[str]) -> list[NLPDetectionResult]:
        """Detect named entities across multiple texts via spaCy batch pipeline.

        Uses ``nlp.pipe()`` for efficient batch processing, amortising the
        pipeline initialisation cost across all input texts.  This is the
        recommended method when scanning multiple records or data-frame
        columns.

        Args:
            texts: A list of text strings to scan.  Empty strings and
                ``None`` values are handled gracefully.

        Returns:
            A list of ``NLPDetectionResult`` objects, one per input text,
            preserving the same order as the input list.

        Example::

            detector = NLPDetector()
            results = detector.detect_batch([
                "John Doe works at IBM.",
                "The weather is nice today.",
                "Mary Johnson lives in Chicago.",
            ])
            assert len(results) == 3
            assert results[0].has_pii is True
            assert results[1].has_pii is False
        """
        batch_start: float = time.perf_counter()
        results: list[NLPDetectionResult] = []

        # Fast-path for empty batch
        if not texts:
            return results

        # Sanitize input: replace None / falsy values with empty strings
        # so that spaCy's pipe() receives only valid str objects.
        sanitized_texts: list[str] = [t if t else "" for t in texts]

        # Process all texts in a single pass through spaCy pipeline.
        # nlp.pipe() is significantly faster than calling nlp(text) in a loop
        # because it batches the underlying Cython operations.
        docs: list[Doc] = list(self._nlp.pipe(sanitized_texts))

        for idx, doc in enumerate(docs):
            text_start: float = time.perf_counter()
            original_text: str | None = texts[idx] if idx < len(texts) else None

            # Handle empty or whitespace-only texts
            if not original_text or not original_text.strip():
                text_elapsed_ms: float = (
                    (time.perf_counter() - text_start) * 1000.0
                )
                results.append(
                    NLPDetectionResult(
                        entities=[],
                        has_pii=False,
                        entity_count=0,
                        processing_time_ms=round(text_elapsed_ms, 2),
                        model_name=self._model_name,
                    )
                )
                continue

            # Build context and extract entities
            context: _DetectionContext = self._build_detection_context(doc)
            detected_entities: list[NLPEntity] = self._extract_entities(
                doc, context
            )

            text_elapsed_ms = (time.perf_counter() - text_start) * 1000.0
            results.append(
                NLPDetectionResult(
                    entities=detected_entities,
                    has_pii=len(detected_entities) > 0,
                    entity_count=len(detected_entities),
                    processing_time_ms=round(text_elapsed_ms, 2),
                    model_name=self._model_name,
                )
            )

        batch_elapsed_ms: float = (time.perf_counter() - batch_start) * 1000.0

        # Log batch processing summary
        total_entities: int = sum(r.entity_count for r in results)
        pii_texts_count: int = sum(1 for r in results if r.has_pii)
        self._logger.info(
            "nlp_batch_detection_complete",
            batch_size=len(texts),
            total_entities=total_entities,
            texts_with_pii=pii_texts_count,
            total_processing_time_ms=round(batch_elapsed_ms, 2),
            model_name=self._model_name,
        )

        return results

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _calculate_confidence(self, entity: Span, doc: Doc) -> float:
        """Calculate a heuristic confidence score for a detected named entity.

        Since spaCy's NER pipeline does not expose per-entity probability
        scores via its default API, this method uses contextual heuristics
        to estimate detection reliability:

        - **PERSON** preceded by honorifics (Mr., Mrs., Dr.) → ``0.95``
        - **PERSON** with multi-token names (first + last) → ``0.85``
        - **GPE/LOC** in address context (at, in, from, near) → ``0.90``
        - **GPE/LOC** with secondary address context → ``0.88``
        - **ORG** followed by corporate suffixes (Inc., Corp.) → ``0.92``
        - **ORG** ending with corporate suffix → ``0.92``
        - **DATE** entities → ``0.85``
        - **NORP** demographic entities → ``0.83``
        - All other recognised entity types → ``0.80`` (base)

        The final score is capped at ``1.0``.

        Args:
            entity: The spaCy ``Span`` representing the detected entity.
                ``entity.start`` provides the token index for context lookup.
            doc: The parent ``Doc`` containing the full tokenised text.
                Used to access surrounding tokens for context analysis.

        Returns:
            A confidence score in the range ``[0.0, 1.0]``.
        """
        base_confidence: float = 0.80
        label: str = entity.label_

        # Delegate to label-specific confidence boosters via static lookup
        label_boost_lookup: dict[str, float] = {
            "DATE": 0.05,
            "NORP": 0.03,
        }
        boost: float = label_boost_lookup.get(label, 0.0)

        if label == "PERSON":
            boost = self._person_confidence_boost(entity)
        elif label in ("GPE", "LOC"):
            boost = self._location_confidence_boost(entity)
        elif label == "ORG":
            boost = self._org_confidence_boost(entity, doc)

        # Cap at 1.0 and round to avoid floating-point precision artefacts
        # (e.g., 0.80 + 0.15 yielding 0.9500000000000001).
        return round(min(base_confidence + boost, 1.0), 4)

    def _person_confidence_boost(self, entity: Span) -> float:
        """Compute confidence boost for PERSON entities.

        Checks for preceding honorifics (Mr., Mrs., Dr.) which strongly
        indicate a genuine person name, and for multi-token names which
        are more reliable than single-token matches.

        Args:
            entity: The spaCy ``Span`` representing the PERSON entity.

        Returns:
            A confidence boost value to add to the base confidence.
        """
        token_start_idx: int = entity.start
        if token_start_idx > 0:
            preceding_text: str = entity.doc[token_start_idx - 1].text.lower()
            if preceding_text in _HONORIFICS:
                return 0.15  # → 0.95

        # Multi-token person names (e.g., "John Smith") are higher
        # confidence than single-token matches.
        if len(entity) >= 2:
            return 0.05  # → 0.85
        return 0.0

    def _location_confidence_boost(self, entity: Span) -> float:
        """Compute confidence boost for GPE/LOC entities.

        Checks for address-context prepositions (at, in, from, near) in the
        one or two tokens preceding the entity, which indicate the entity
        appears in a genuine geographic context.

        Args:
            entity: The spaCy ``Span`` representing the GPE/LOC entity.

        Returns:
            A confidence boost value to add to the base confidence.
        """
        token_start_idx: int = entity.start
        if token_start_idx > 0:
            preceding_text: str = entity.doc[token_start_idx - 1].text.lower()
            if preceding_text in _ADDRESS_PREPOSITIONS:
                return 0.10  # → 0.90

        # Two tokens back (e.g., "lives in New York")
        if token_start_idx > 1:
            two_back_text: str = entity.doc[token_start_idx - 2].text.lower()
            if two_back_text in _ADDRESS_PREPOSITIONS:
                return 0.08  # → 0.88
        return 0.0

    def _org_confidence_boost(self, entity: Span, doc: Doc) -> float:
        """Compute confidence boost for ORG entities.

        Checks for corporate suffixes (Inc., Corp., Ltd., LLC) following the
        entity or as the entity's own trailing token, which strongly indicate
        a genuine organization name.

        Args:
            entity: The spaCy ``Span`` representing the ORG entity.
            doc: The parent ``Doc`` for looking up tokens after the entity.

        Returns:
            A confidence boost value to add to the base confidence.
        """
        token_end_idx: int = entity.start + len(entity)
        if token_end_idx < len(doc):
            following_text: str = doc[token_end_idx].text.lower()
            if following_text in _CORPORATE_SUFFIXES:
                return 0.12  # → 0.92

        # Check whether the entity's own last token is a suffix
        if len(entity) > 0:
            last_token_text: str = entity[-1].text.lower()
            if last_token_text in _CORPORATE_SUFFIXES:
                return 0.12  # → 0.92
        return 0.0

    def _is_pii_entity(self, label: str) -> bool:
        """Check whether a spaCy entity label is classified as PII-relevant.

        Membership is determined by the instance-level
        ``PII_RELEVANT_ENTITIES`` set, which defaults to
        ``{"PERSON", "GPE", "LOC", "ORG"}`` but may be overridden via the
        ``pii_entity_types`` constructor parameter.

        Args:
            label: The spaCy NER label string (e.g., ``"PERSON"``,
                ``"CARDINAL"``).

        Returns:
            ``True`` if the label is in the ``PII_RELEVANT_ENTITIES`` set;
            ``False`` otherwise.

        Example::

            detector = NLPDetector()
            assert detector._is_pii_entity("PERSON") is True
            assert detector._is_pii_entity("CARDINAL") is False
        """
        return label in self.PII_RELEVANT_ENTITIES

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_detection_context(self, doc: Doc) -> _DetectionContext:
        """Scan document tokens to build a detection context for confidence.

        Iterates over every token in *doc* once to record the positions of
        honorifics and address-related prepositions.  The resulting
        ``_DetectionContext`` is used by ``_calculate_confidence`` when
        scoring each entity.

        Args:
            doc: The spaCy ``Doc`` produced by the NLP pipeline.

        Returns:
            A populated ``_DetectionContext`` instance.
        """
        context = _DetectionContext(text_length=len(doc.text))

        for token_idx, token in enumerate(doc):
            lower_text: str = token.text.lower()
            if lower_text in _HONORIFICS:
                context.honorific_positions.append(token_idx)
            if lower_text in _ADDRESS_PREPOSITIONS:
                context.preposition_positions.append(token_idx)

        return context

    def _extract_entities(
        self,
        doc: Doc,
        context: _DetectionContext,
    ) -> list[NLPEntity]:
        """Extract, filter, score, and map entities from a processed document.

        Iterates ``doc.ents``, keeps only PII-relevant labels, assigns a
        heuristic confidence score, applies the confidence threshold, maps
        the spaCy label to a PII type category, and returns structured
        ``NLPEntity`` objects.

        Args:
            doc: The spaCy ``Doc`` produced by the NLP pipeline.
            context: Pre-computed ``_DetectionContext`` with token-level
                metadata used during confidence scoring.

        Returns:
            A list of ``NLPEntity`` instances that passed all filters.
        """
        detected_entities: list[NLPEntity] = []

        for ent in doc.ents:
            entity_label: str = ent.label_
            entity_text: str = ent.text

            # Skip non-PII entity types
            if not self._is_pii_entity(entity_label):
                continue

            # Heuristic confidence scoring
            confidence: float = self._calculate_confidence(ent, doc)

            # Apply confidence threshold
            if confidence < self._confidence_threshold:
                continue

            # Map spaCy label → PII type category
            pii_type: str = self.ENTITY_TYPE_MAP.get(entity_label, entity_label)

            # Record position in context for potential downstream use
            context.entity_positions.append((ent.start_char, ent.end_char))

            detected_entities.append(
                NLPEntity(
                    text=entity_text,
                    label=entity_label,
                    start_char=ent.start_char,
                    end_char=ent.end_char,
                    confidence=confidence,
                    pii_type=pii_type,
                )
            )

        return detected_entities
