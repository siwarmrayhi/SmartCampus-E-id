from typing import Optional

from pydantic import BaseModel


class EnrollResponse(BaseModel):
    subject_id: str
    embedding: list[float]


class FaceResult(BaseModel):
    """Résultat pour UN visage détecté sur l'image envoyée."""
    bbox: Optional[list[int]] = None  # [left, top, width, height] en pixels
    vivant: Optional[bool] = None
    resultat: Optional[str] = None
    subject_id: Optional[str] = None
    confiance: Optional[float] = None
    similarite_max: Optional[float] = None
    raison: Optional[str] = None
    avertissement: Optional[str] = None
    embedding: Optional[list[float]] = None


class RecognizeResponse(BaseModel):
    visages_detectes: int
    personnes_detectees: int
    presence_non_identifiee: bool
    resultats: list[FaceResult]


class LoadEmbeddingsRequest(BaseModel):
    embeddings: dict[str, list[float]]


class HealthResponse(BaseModel):
    status: str
    personnes_en_cache: int


# ============================================================
# NOUVEAU — PHASE 1 : TRACKING
#
# Volontairement séparé de FaceResult/RecognizeResponse : /track ne
# contient AUCUNE identité, juste des positions et des track_id locaux
# à une caméra. L'association identité <-> track_id arrive en Phase 2,
# pas ici.
# ============================================================

class TrackItem(BaseModel):
    """Une personne suivie sur cette frame — pas encore identifiée."""
    track_id: Optional[int] = None  # None tant que le track n'est pas confirmé
    bbox: list[int]  # [left, top, width, height]
    confiance: Optional[float] = None
    etat: str  # "confirme" | "tentative"


class TrackResponse(BaseModel):
    camera_id: str
    tracks: list[TrackItem]