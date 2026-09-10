import logging
import os
import time
from typing import Optional

import cv2
import numpy as np
import mediapipe as mp
from insightface.app import FaceAnalysis
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

from app.services import liveness
from app.services import embedding_candidates


logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION INSIGHTFACE
# ============================================================

# Résolution utilisée par le détecteur InsightFace.
#
# 1280x1280 permet de mieux détecter les petits visages
# présents dans les images de caméra de surveillance.
#
# Attention : plus cette valeur est grande, plus le traitement
# est lent.
DET_SIZE = (1024, 1024)


_face_app = FaceAnalysis(
    name="buffalo_l",
    providers=["CPUExecutionProvider"]
)

_face_app.prepare(
    ctx_id=-1,
    det_size=DET_SIZE
)


# ============================================================
# DÉTECTION DES PERSONNES — PHASE 1.5 (tentative 2)
#
# HISTORIQUE :
# 1) HOG (Phase 1) — trop de faux positifs sur objets (téléviseurs,
#    cartons, chaises), confirmé sur vraie vidéo de test.
# 2) SSD MobileNet via cv2.dnn (tentative 1) — écarté : erreur
#    "Const input blob for weights not found", un problème
#    d'appariement fragile entre poids TensorFlow (2017/2018) et
#    fichier de config .pbtxt, jamais résolu malgré 2 essais avec des
#    versions différentes.
#
# SOLUTION RETENUE : MediaPipe Object Detector (Google, licence
# Apache 2.0, vérifiée). Avantages déterminants :
#   - Un SEUL fichier modèle auto-suffisant (.tflite) — plus aucun
#     risque d'appariement entre deux fichiers de sources séparées.
#   - Infrastructure de téléchargement activement maintenue par
#     Google aujourd'hui, contrairement aux fichiers TensorFlow 2017
#     largement abandonnés.
#   - Filtrage par NOM de catégorie ("person"), pas par ID numérique
#     fragile.
#
# IMPORTANT :
# Ce détecteur NE reconnaît PAS l'identité d'une personne — il détecte
# uniquement une présence humaine, même lorsque le visage n'est pas
# exploitable. Le format de sortie ([left, top, width, height]) est
# IDENTIQUE aux versions précédentes : aucune modification nécessaire
# dans tracker.py ni ailleurs.
# ============================================================

_MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "models")
_MEDIAPIPE_MODEL_PATH = os.path.join(_MODELS_DIR, "efficientdet_lite0.tflite")

PERSON_DET_CONFIDENCE_THRESHOLD = 0.5

_base_options = mp_python.BaseOptions(model_asset_path=_MEDIAPIPE_MODEL_PATH)
_detector_options = mp_vision.ObjectDetectorOptions(
    base_options=_base_options,
    running_mode=mp_vision.RunningMode.IMAGE,
    category_allowlist=["person"],
    score_threshold=PERSON_DET_CONFIDENCE_THRESHOLD,
)
_person_detector = mp_vision.ObjectDetector.create_from_options(_detector_options)


def detect_persons(image_bgr: np.ndarray) -> list:


    # MediaPipe attend du RGB, notre pipeline travaille en BGR (OpenCV)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)

    resultat = _person_detector.detect(mp_image)

    resultats = []

    for detection in resultat.detections:
        bbox = detection.bounding_box
        confiance = detection.categories[0].score if detection.categories else None

        resultats.append({
            "bbox": [int(bbox.origin_x), int(bbox.origin_y), int(bbox.width), int(bbox.height)],
            "confidence": float(confiance) if confiance is not None else None,
        })

        logger.info(
            f"DETECT_PERSONS : personne détectée, confiance={round(confiance, 3) if confiance else None}, "
            f"bbox=[{bbox.origin_x},{bbox.origin_y},{bbox.width},{bbox.height}]"
        )

    return resultats


# ============================================================
# STOCKAGE DES EMBEDDINGS
# ============================================================

EMBEDDINGS_STORE: dict[str, list[float]] = {}


# ============================================================
# SEUILS
# ============================================================

MATCH_THRESHOLD = 0.4

DET_SCORE_THRESHOLD = 0.5

FRONTAL_RATIO_THRESHOLD = 0.35


# En dessous de cette largeur, le visage est considéré
# comme petit et donc moins fiable.
#
# IMPORTANT :
# On ne rejette PAS automatiquement le visage.
# On tente quand même la reconnaissance.

MIN_FACE_WIDTH_PX = 60


# ============================================================
# OUTILS
# ============================================================

def estimate_frontal_ratio(kps: np.ndarray) -> float:
    """
    Estime approximativement si le visage est frontal.

    Plus le ratio est proche de 1, plus les distances
    oeil gauche-nez et oeil droit-nez sont similaires.
    """

    eye_left, eye_right, nose = kps[0], kps[1], kps[2]

    dist_left = np.linalg.norm(nose - eye_left)
    dist_right = np.linalg.norm(nose - eye_right)

    if max(dist_left, dist_right) == 0:
        return 0.0

    return float(
        min(dist_left, dist_right)
        / max(dist_left, dist_right)
    )


def bytes_to_image(file_bytes: bytes) -> np.ndarray:
    """
    Convertit les bytes reçus par FastAPI en image OpenCV.
    """

    array = np.frombuffer(
        file_bytes,
        dtype=np.uint8
    )

    image = cv2.imdecode(
        array,
        cv2.IMREAD_COLOR
    )

    if image is None:

        logger.error(
            "❌ cv2.imdecode() a échoué : image illisible"
        )

    else:

        logger.info(
            "Image décodée correctement : "
            f"shape={image.shape}, "
            f"dtype={image.dtype}"
        )

    return image


def cosine_similarity(
    a: np.ndarray,
    b: np.ndarray
) -> float:

    a_norm = a / np.linalg.norm(a)
    b_norm = b / np.linalg.norm(b)

    return float(
        np.dot(a_norm, b_norm)
    )


# ============================================================
# ENROLLMENT
# ============================================================

def compute_average_embedding(
    images_bytes: list[bytes]
) -> Optional[list[float]]:

    embeddings = []

    for i, file_bytes in enumerate(images_bytes):

        image = bytes_to_image(file_bytes)

        if image is None:

            logger.error(
                f"Photo {i + 1}: image invalide"
            )

            continue

        logger.info(
            f"Photo {i + 1}: lancement détection "
            f"InsightFace sur "
            f"{image.shape[1]}x{image.shape[0]}"
        )

        start_detection = time.perf_counter()

        faces = _face_app.get(image)

        detection_time = (
            time.perf_counter()
            - start_detection
        )

        logger.info(
            f"Photo {i + 1}: "
            f"{len(faces)} visage(s) détecté(s) "
            f"en {detection_time:.3f}s"
        )

        for j, face in enumerate(faces):

            logger.info(
                f"Photo {i + 1} - visage {j + 1}: "
                f"bbox={face.bbox.tolist()}, "
                f"det_score={float(face.det_score):.4f}"
            )

        if faces:

            
            embeddings.append(
                faces[0].embedding
            )

        else:

            logger.warning(
                f"Aucun visage détecté sur "
                f"la photo {i + 1}, ignorée."
            )

    if not embeddings:
        return None

    return np.mean(
        embeddings,
        axis=0
    ).tolist()


# ============================================================
# LIVENESS
# ============================================================

def check_liveness(
    image_bgr: np.ndarray,
    bbox: list
) -> bool:

    try:

        return liveness.is_real_face(
            image_bgr,
            bbox
        )

    except Exception as e:

        logger.error(
            f"Erreur lors de la vérification "
            f"de vivacité : {e}"
        )

        return False


# ============================================================
# RECONNAISSANCE
# ============================================================

def recognize_face(
    file_bytes: bytes
) -> dict:

    """
    Détecte, vérifie la vivacité et identifie
    tous les visages présents dans l'image.

    Cette version ajoute surtout des mesures de temps
    afin de tester les performances du système.

    Les temps mesurés sont :

        1. temps total
        2. temps de détection InsightFace
        3. temps de détection des personnes HOG
        4. temps de liveness/reconnaissance
    """

    # --------------------------------------------------------
    # TIMER TOTAL
    # --------------------------------------------------------

    start_total = time.perf_counter()


    # --------------------------------------------------------
    # DÉCODAGE IMAGE
    # --------------------------------------------------------

    start_decode = time.perf_counter()

    image_bgr = bytes_to_image(file_bytes)

    decode_time = (
        time.perf_counter()
        - start_decode
    )

    if image_bgr is None:

        raise ValueError(
            "Image impossible à décoder"
        )


    logger.info(
        f"RECOGNIZE : "
        f"image={image_bgr.shape[1]}x"
        f"{image_bgr.shape[0]}"
    )

    logger.info(
        f"RECOGNIZE : temps décodage = "
        f"{decode_time:.3f}s"
    )


    # --------------------------------------------------------
    # DÉTECTION INSIGHTFACE
    # --------------------------------------------------------

    start_detection = time.perf_counter()

    faces = _face_app.get(image_bgr)

    detection_time = (
        time.perf_counter()
        - start_detection
    )


    logger.info(
        f"RECOGNIZE : "
        f"{len(faces)} visage(s) détecté(s)"
    )

    logger.info(
        f"RECOGNIZE : "
        f"temps détection InsightFace = "
        f"{detection_time:.3f}s"
    )


    # Informations détaillées sur les visages

    for i, face in enumerate(faces):

        logger.info(
            f"RECOGNIZE : visage {i + 1} | "
            f"bbox={face.bbox.tolist()} | "
            f"det_score={float(face.det_score):.4f}"
        )


    # --------------------------------------------------------
    # DÉTECTION PERSONNES HOG
    # --------------------------------------------------------

    start_person_detection = time.perf_counter()

    personnes = detect_persons(image_bgr)

    person_detection_time = (
        time.perf_counter()
        - start_person_detection
    )


    logger.info(
        f"RECOGNIZE : "
        f"{len(personnes)} personne(s) détectée(s) "
        f"par HOG"
    )

    logger.info(
        f"RECOGNIZE : "
        f"temps détection personnes HOG = "
        f"{person_detection_time:.3f}s"
    )


    # --------------------------------------------------------
    # EMBEDDINGS CONNUS
    # --------------------------------------------------------

    known_ids = list(
        EMBEDDINGS_STORE.keys()
    )

    known_embeddings = (
        [
            np.array(
                EMBEDDINGS_STORE[sid]
            )
            for sid in known_ids
        ]
        if known_ids
        else []
    )


    # --------------------------------------------------------
    # TRAITEMENT DES VISAGES
    # --------------------------------------------------------

    resultats = []

    start_face_processing = time.perf_counter()


    for face in faces:

        x1, y1, x2, y2 = (
            face.bbox.astype(int)
        )

        bbox = [
            int(x1),
            int(y1),
            int(x2 - x1),
            int(y2 - y1)
        ]


        # ----------------------------------------------------
        # SCORE DE DÉTECTION
        # ----------------------------------------------------

        if (
            face.det_score
            < DET_SCORE_THRESHOLD
        ):

            resultats.append({

                "bbox": bbox,

                "vivant": None,

                "resultat":
                    "detection_incertaine",

                "raison":
                    f"score de détection "
                    f"{round(float(face.det_score), 3)} "
                    f"sous le seuil "
                    f"{DET_SCORE_THRESHOLD}",
            })

            continue


        # ----------------------------------------------------
        # TAILLE DU VISAGE
        # ----------------------------------------------------

        face_width_px = bbox[2]

        visage_petit = (
            face_width_px
            < MIN_FACE_WIDTH_PX
        )


        # ----------------------------------------------------
        # ANGLE DU VISAGE
        # ----------------------------------------------------

        frontal_ratio = (
            estimate_frontal_ratio(
                face.kps
            )
        )


        if (
            frontal_ratio
            < FRONTAL_RATIO_THRESHOLD
        ):

            resultats.append({

                "bbox": bbox,

                "vivant": None,

                "resultat":
                    "angle_trop_marque",

                "raison":
                    f"ratio de frontalité "
                    f"{round(frontal_ratio, 3)} "
                    f"sous le seuil "
                    f"{FRONTAL_RATIO_THRESHOLD}",
            })

            continue


        # ----------------------------------------------------
        # LIVENESS
        # ----------------------------------------------------

        start_liveness = time.perf_counter()

        vivant = check_liveness(
            image_bgr,
            bbox
        )

        liveness_time = (
            time.perf_counter()
            - start_liveness
        )


        logger.info(
            f"RECOGNIZE : "
            f"liveness = {vivant} "
            f"en {liveness_time:.3f}s"
        )


        if not vivant:

            resultats.append({

                "bbox": bbox,

                "vivant": False,

                "resultat":
                    "spoof_detecte"
            })

            continue


        # ----------------------------------------------------
        # RECONNAISSANCE
        # ----------------------------------------------------

        if not known_embeddings:

            resultat = {

                "vivant": True,

                "resultat":
                    "inconnu",

                "raison":
                    "aucune personne enrôlée",

                "embedding":
                    face.embedding.tolist(),
            }

        else:

            similarities = [

                cosine_similarity(
                    emb,
                    face.embedding
                )

                for emb
                in known_embeddings
            ]


            best_index = int(
                np.argmax(similarities)
            )

            best_similarity = (
                similarities[best_index]
            )


            if (
                best_similarity
                >= MATCH_THRESHOLD
            ):

                resultat = {

                    "vivant": True,

                    "resultat":
                        "reconnu",

                    "subject_id":
                        known_ids[best_index],

                    "confiance":
                        round(
                            best_similarity,
                            3
                        ),

                    "embedding":
                        face.embedding.tolist(),
                }

            else:

                resultat = {

                    "vivant": True,

                    "resultat":
                        "inconnu",

                    "similarite_max":
                        round(
                            best_similarity,
                            3
                        ),

                    "embedding":
                        face.embedding.tolist(),
                }


        # ----------------------------------------------------
        # BBOX
        # ----------------------------------------------------

        resultat["bbox"] = bbox


        # ----------------------------------------------------
        # AVERTISSEMENT PETIT VISAGE
        # ----------------------------------------------------

        if visage_petit:

            resultat["avertissement"] = (

                f"visage petit "
                f"({face_width_px}px, "
                f"sous "
                f"{MIN_FACE_WIDTH_PX}px) "
                f"— fiabilité réduite, "
                f"résultat à confirmer"
            )

        # ----------------------------------------------------
        # CANDIDAT D'EMBEDDING — amélioration progressive contrôlée
        # (voir embedding_candidates.py — ne modifie JAMAIS le profil
        # directement, stocke juste un candidat en attente)
        # ----------------------------------------------------

        if resultat.get("resultat") == "reconnu" and not visage_petit:

            embedding_candidates.evaluer_candidat(
                subject_id=known_ids[best_index],
                embedding=(
                    face.embedding.tolist()
                    if hasattr(face.embedding, "tolist")
                    else list(face.embedding)
                ),
                embedding_profil_actuel=known_embeddings[best_index],
                confiance=best_similarity,
                face_size=face_width_px,
                vivant=resultat.get("vivant", False),
            )

        resultats.append(resultat)


    face_processing_time = (
        time.perf_counter()
        - start_face_processing
    )


    logger.info(
        f"RECOGNIZE : "
        f"temps traitement des visages = "
        f"{face_processing_time:.3f}s"
    )


    # --------------------------------------------------------
    # PERSONNES NON IDENTIFIÉES
    # --------------------------------------------------------

    visages_avec_identite = sum(

        1

        for r in resultats

        if r.get("resultat")
        in (
            "reconnu",
            "inconnu",
            "spoof_detecte"
        )
    )


    presence_non_identifiee = (
        len(personnes)
        > visages_avec_identite
    )


    # --------------------------------------------------------
    # TEMPS TOTAL
    # --------------------------------------------------------

    total_time = (
        time.perf_counter()
        - start_total
    )


    logger.info(
        "================================================"
    )

    logger.info(
        f"PERFORMANCE : "
        f"temps TOTAL = {total_time:.3f}s"
    )

    logger.info(
        f"PERFORMANCE : "
        f"InsightFace = {detection_time:.3f}s"
    )

    logger.info(
        f"PERFORMANCE : "
        f"HOG = {person_detection_time:.3f}s"
    )

    logger.info(
        f"PERFORMANCE : "
        f"traitement visages = "
        f"{face_processing_time:.3f}s"
    )

    logger.info(
        "================================================"
    )


    # --------------------------------------------------------
    # RÉSULTAT
    # --------------------------------------------------------

    return {

        "visages_detectes":
            len(faces),

        "personnes_detectees":
            len(personnes),

        "presence_non_identifiee":
            presence_non_identifiee,

        "resultats":
            resultats,
    }