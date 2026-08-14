from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from random import Random
from statistics import median
from typing import Iterable, Mapping, Sequence


DistanceMatrix = Mapping[str, Mapping[str, float]]


@dataclass(frozen=True)
class ClusterResult:
    """A deterministic clustering over real facility identifiers."""

    method: str
    k: int
    facilities: tuple[str, ...]
    labels: tuple[int, ...]
    medoids: tuple[str, ...]
    distance_to_medoid: tuple[float, ...]
    silhouette: float
    within_dissimilarity: float
    cluster_sizes: tuple[int, ...]
    stability_median_ari: float | None = None

    def __post_init__(self) -> None:
        if len(self.facilities) != len(self.labels):
            raise ValueError("facilities and labels must have the same length")
        if len(self.facilities) != len(self.distance_to_medoid):
            raise ValueError("facilities and distance_to_medoid must have the same length")
        if self.k != len(self.medoids) or self.k != len(self.cluster_sizes):
            raise ValueError("k must match medoids and cluster_sizes")

    @property
    def label_by_facility(self) -> dict[str, int]:
        return dict(zip(self.facilities, self.labels))

    @property
    def medoid_by_cluster(self) -> dict[int, str]:
        return dict(enumerate(self.medoids))

    def membership_rows(self) -> list[dict[str, object]]:
        medoids = self.medoid_by_cluster
        return [
            {
                "method": self.method,
                "k": self.k,
                "facility": facility,
                "cluster": label,
                "medoid": medoids[label],
                "distance_to_medoid_km": distance,
            }
            for facility, label, distance in zip(
                self.facilities, self.labels, self.distance_to_medoid
            )
        ]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_dissimilarity(facilities: Sequence[str], matrix: DistanceMatrix) -> None:
    """Validate the supplied direct-distance matrix without metric closure."""

    values = tuple(facilities)
    if len(values) < 2 or len(values) != len(set(values)):
        raise ValueError("facilities must contain at least two unique identifiers")
    for left in values:
        if left not in matrix:
            raise ValueError(f"missing distance row for {left}")
        for right in values:
            if right not in matrix[left]:
                raise ValueError(f"missing distance {left}->{right}")
            distance = float(matrix[left][right])
            if distance < 0:
                raise ValueError(f"negative distance {left}->{right}")
            if left == right and distance != 0:
                raise ValueError(f"diagonal distance for {left} must be zero")
            if abs(distance - float(matrix[right][left])) > 1e-9:
                raise ValueError(f"distance matrix is not symmetric at {left}/{right}")


def _canonical_labels(clusters: Iterable[Iterable[int]], size: int) -> tuple[int, ...]:
    ordered = sorted(tuple(sorted(cluster)) for cluster in clusters)
    labels = [-1] * size
    for label, cluster in enumerate(ordered):
        for index in cluster:
            labels[index] = label
    if any(label < 0 for label in labels):
        raise ValueError("clusters do not cover every facility")
    return tuple(labels)


def _cluster_medoid(
    members: Sequence[int], facilities: Sequence[str], matrix: DistanceMatrix
) -> int:
    return min(
        members,
        key=lambda candidate: (
            sum(float(matrix[facilities[candidate]][facilities[other]]) for other in members),
            facilities[candidate],
        ),
    )


def silhouette_score(
    facilities: Sequence[str], labels: Sequence[int], matrix: DistanceMatrix
) -> float:
    """Compute the standard silhouette directly from a dissimilarity matrix."""

    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(label, []).append(index)
    if len(groups) < 2:
        raise ValueError("silhouette requires at least two clusters")
    scores: list[float] = []
    for index, label in enumerate(labels):
        own = groups[label]
        if len(own) == 1:
            scores.append(0.0)
            continue
        node = facilities[index]
        intra = sum(
            float(matrix[node][facilities[other]]) for other in own if other != index
        ) / (len(own) - 1)
        nearest_other = min(
            sum(float(matrix[node][facilities[other]]) for other in members) / len(members)
            for other_label, members in groups.items()
            if other_label != label
        )
        denominator = max(intra, nearest_other)
        scores.append((nearest_other - intra) / denominator if denominator else 0.0)
    return sum(scores) / len(scores)


def _result_from_labels(
    method: str,
    facilities: Sequence[str],
    labels: Sequence[int],
    matrix: DistanceMatrix,
) -> ClusterResult:
    facility_tuple = tuple(facilities)
    label_tuple = tuple(labels)
    groups = {
        label: tuple(index for index, value in enumerate(label_tuple) if value == label)
        for label in sorted(set(label_tuple))
    }
    if tuple(groups) != tuple(range(len(groups))):
        label_tuple = _canonical_labels(groups.values(), len(facility_tuple))
        return _result_from_labels(method, facility_tuple, label_tuple, matrix)
    medoid_indices = tuple(
        _cluster_medoid(groups[label], facility_tuple, matrix) for label in groups
    )
    medoids = tuple(facility_tuple[index] for index in medoid_indices)
    distances = tuple(
        float(matrix[facility][medoids[label]])
        for facility, label in zip(facility_tuple, label_tuple)
    )
    return ClusterResult(
        method=method,
        k=len(groups),
        facilities=facility_tuple,
        labels=label_tuple,
        medoids=medoids,
        distance_to_medoid=distances,
        silhouette=silhouette_score(facility_tuple, label_tuple, matrix),
        within_dissimilarity=sum(distances),
        cluster_sizes=tuple(len(groups[label]) for label in groups),
    )


def pam_k_medoids(
    facilities: Sequence[str], matrix: DistanceMatrix, k: int
) -> ClusterResult:
    """Deterministic PAM BUILD + SWAP using only pairwise dissimilarities."""

    facility_tuple = tuple(facilities)
    validate_dissimilarity(facility_tuple, matrix)
    size = len(facility_tuple)
    if not 2 <= k < size:
        raise ValueError(f"k must satisfy 2 <= k < {size}")

    def assignment_cost(medoids: Sequence[int]) -> float:
        return sum(
            min(float(matrix[facility_tuple[index]][facility_tuple[medoid]]) for medoid in medoids)
            for index in range(size)
        )

    medoids = [
        min(
            range(size),
            key=lambda index: (
                sum(float(matrix[facility_tuple[index]][other]) for other in facility_tuple),
                facility_tuple[index],
            ),
        )
    ]
    while len(medoids) < k:
        current = [
            min(float(matrix[facility_tuple[index]][facility_tuple[medoid]]) for medoid in medoids)
            for index in range(size)
        ]
        candidates = [index for index in range(size) if index not in medoids]
        chosen = min(
            candidates,
            key=lambda candidate: (
                -sum(
                    current[index]
                    - min(
                        current[index],
                        float(matrix[facility_tuple[index]][facility_tuple[candidate]]),
                    )
                    for index in range(size)
                ),
                facility_tuple[candidate],
            ),
        )
        medoids.append(chosen)

    best_cost = assignment_cost(medoids)
    while True:
        best_swap: tuple[float, tuple[str, ...], list[int]] | None = None
        non_medoids = [index for index in range(size) if index not in medoids]
        for position in range(k):
            for candidate in non_medoids:
                trial = list(medoids)
                trial[position] = candidate
                cost = assignment_cost(trial)
                key = (cost, tuple(sorted(facility_tuple[index] for index in trial)), trial)
                if cost + 1e-9 < best_cost and (best_swap is None or key[:2] < best_swap[:2]):
                    best_swap = key
        if best_swap is None:
            break
        best_cost, _, medoids = best_swap

    raw_labels = tuple(
        min(
            range(k),
            key=lambda label: (
                float(matrix[facility_tuple[index]][facility_tuple[medoids[label]]]),
                facility_tuple[medoids[label]],
            ),
        )
        for index in range(size)
    )
    labels = _canonical_labels(
        (
            (index for index, label in enumerate(raw_labels) if label == medoid_label)
            for medoid_label in range(k)
        ),
        size,
    )
    return _result_from_labels("pam", facility_tuple, labels, matrix)


def average_linkage(
    facilities: Sequence[str], matrix: DistanceMatrix, k: int
) -> ClusterResult:
    """Deterministic average-linkage clustering over a precomputed matrix."""

    facility_tuple = tuple(facilities)
    validate_dissimilarity(facility_tuple, matrix)
    size = len(facility_tuple)
    if not 2 <= k < size:
        raise ValueError(f"k must satisfy 2 <= k < {size}")
    clusters: list[tuple[int, ...]] = [(index,) for index in range(size)]
    while len(clusters) > k:
        best: tuple[float, tuple[str, ...], tuple[str, ...], int, int] | None = None
        for left_index, left in enumerate(clusters):
            for right_index in range(left_index + 1, len(clusters)):
                right = clusters[right_index]
                distance = sum(
                    float(matrix[facility_tuple[a]][facility_tuple[b]])
                    for a in left
                    for b in right
                ) / (len(left) * len(right))
                key = (
                    distance,
                    tuple(facility_tuple[index] for index in left),
                    tuple(facility_tuple[index] for index in right),
                    left_index,
                    right_index,
                )
                if best is None or key < best:
                    best = key
        assert best is not None
        _, _, _, left_index, right_index = best
        merged = tuple(sorted(clusters[left_index] + clusters[right_index]))
        clusters = [
            cluster
            for index, cluster in enumerate(clusters)
            if index not in {left_index, right_index}
        ]
        clusters.append(merged)
        clusters.sort()
    labels = _canonical_labels(clusters, size)
    return _result_from_labels("average", facility_tuple, labels, matrix)


def adjusted_rand_index(left: Sequence[int], right: Sequence[int]) -> float:
    if len(left) != len(right):
        raise ValueError("label vectors must have equal length")
    size = len(left)
    if size < 2:
        return 1.0

    def choose_two(value: int) -> int:
        return value * (value - 1) // 2

    left_groups: dict[int, set[int]] = {}
    right_groups: dict[int, set[int]] = {}
    for index, label in enumerate(left):
        left_groups.setdefault(label, set()).add(index)
    for index, label in enumerate(right):
        right_groups.setdefault(label, set()).add(index)
    index_sum = sum(
        choose_two(len(a & b)) for a in left_groups.values() for b in right_groups.values()
    )
    left_sum = sum(choose_two(len(group)) for group in left_groups.values())
    right_sum = sum(choose_two(len(group)) for group in right_groups.values())
    total = choose_two(size)
    expected = left_sum * right_sum / total
    maximum = (left_sum + right_sum) / 2
    denominator = maximum - expected
    return (index_sum - expected) / denominator if denominator else 1.0


def _jittered_matrix(
    facilities: Sequence[str], matrix: DistanceMatrix, random: Random, jitter: float
) -> dict[str, dict[str, float]]:
    result = {facility: {} for facility in facilities}
    for left_index, left in enumerate(facilities):
        result[left][left] = 0.0
        for right in facilities[left_index + 1 :]:
            value = float(matrix[left][right]) * random.uniform(1 - jitter, 1 + jitter)
            result[left][right] = value
            result[right][left] = value
    return result


def with_stability(
    result: ClusterResult,
    matrix: DistanceMatrix,
    *,
    repeats: int = 50,
    jitter: float = 0.05,
    seed: int = 0,
) -> ClusterResult:
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if not 0 <= jitter < 1:
        raise ValueError("jitter must satisfy 0 <= jitter < 1")
    random = Random(seed)
    scores: list[float] = []
    for _ in range(repeats):
        perturbed = _jittered_matrix(result.facilities, matrix, random, jitter)
        candidate = (
            pam_k_medoids(result.facilities, perturbed, result.k)
            if result.method == "pam"
            else average_linkage(result.facilities, perturbed, result.k)
        )
        scores.append(adjusted_rand_index(result.labels, candidate.labels))
    return replace(result, stability_median_ari=median(scores))


def cluster_sweep(
    facilities: Sequence[str],
    matrix: DistanceMatrix,
    *,
    methods: Sequence[str] = ("pam", "average"),
    k_values: Iterable[int] = range(2, 11),
    stability_repeats: int = 50,
    jitter: float = 0.05,
    seed: int = 0,
) -> tuple[ClusterResult, ...]:
    results: list[ClusterResult] = []
    for method in methods:
        if method not in {"pam", "average"}:
            raise ValueError(f"unsupported clustering method: {method}")
        for k in k_values:
            fitted = (
                pam_k_medoids(facilities, matrix, k)
                if method == "pam"
                else average_linkage(facilities, matrix, k)
            )
            results.append(
                with_stability(
                    fitted,
                    matrix,
                    repeats=stability_repeats,
                    jitter=jitter,
                    seed=seed,
                )
            )
    return tuple(results)
