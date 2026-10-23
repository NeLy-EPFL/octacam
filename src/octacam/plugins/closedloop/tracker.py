"""Ball tracking for the closed loop: a ring template (the ball's bright rim
between a dark center and a dark halo) matched by normalized cross-correlation
in each tracked corridor's column, near the ball's last position.

Costs about 0.2 ms per corridor per 2048x2048 Mono8 frame. Coordinates are full
resolution pixels of the raw camera image.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# The ball's outer rim radius at the rig's magnification (2048x2048 GS3 over the
# six-corridor arena).
BALL_RADIUS_PX = 30.0
# Match scores at the tracking scale. A clear ball scores ~0.8 (1st percentile
# 0.77); the arena's ring-shaped gates and a fly beside its ball up to ~0.7. So
# only a ball scores FIND_SCORE, which a ball found anywhere in its column must
# reach; near its last position a match is taken from TRACK_SCORE, and a weaker
# one (legs over the ball) is kept down to KEEP_SCORE.
FIND_SCORE = 0.75
TRACK_SCORE = 0.6
KEEP_SCORE = 0.4
# The whole-frame detection's candidates, at quarter resolution, where balls
# score 0.65-0.83 and a fly or a gate up to 0.68: each is confirmed at the
# tracking scale, where a ball beside the arena's triangle markers scores only
# ~0.70 and a gate ~0.62.
DETECT_SCORE = 0.6
CONFIRM_SCORE = 0.65
_TRACK_SCALE = 2
_DETECT_SCALE = 4


def ring_template(radius_px: float, scale: int) -> np.ndarray:
    """The ball as seen from above, downsampled by `scale`: a bright rim from
    0.4 to 1.0 radius, a dimmer center, a dark halo to 1.35 radius.
    """
    r = radius_px / scale
    size = int(np.ceil(2.7 * r)) | 1
    center = (size - 1) / 2
    yy, xx = np.mgrid[:size, :size]
    d = np.hypot(xx - center, yy - center) / r
    template = np.full((size, size), 0.25, np.float32)
    template[d < 1.35] = 0.0
    template[(d >= 0.4) & (d < 1.0)] = 1.0
    template[d < 0.4] = 0.35
    return template


@dataclass(frozen=True)
class Ball:
    x: float
    y: float
    score: float


def _downsample(image: np.ndarray, scale: int) -> np.ndarray:
    h, w = image.shape[0] // scale, image.shape[1] // scale
    return cv2.resize(
        image[: h * scale, : w * scale], (w, h), interpolation=cv2.INTER_AREA
    )


def _peak(scores: np.ndarray) -> tuple[float, float, float]:
    """`(x, y, score)` of the best match, refined to a sub-pixel by a parabola
    through its neighbors.
    """
    y, x = np.unravel_index(int(np.argmax(scores)), scores.shape)

    def offset(a: float, b: float, c: float) -> float:
        den = a - 2 * b + c
        return 0.0 if den == 0 else float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))

    dy = (
        offset(scores[y - 1, x], scores[y, x], scores[y + 1, x])
        if 0 < y < scores.shape[0] - 1
        else 0.0
    )
    dx = (
        offset(scores[y, x - 1], scores[y, x], scores[y, x + 1])
        if 0 < x < scores.shape[1] - 1
        else 0.0
    )
    return x + dx, y + dy, float(scores[y, x])


def detect_balls(
    frame: np.ndarray, count: int, radius_px: float = BALL_RADIUS_PX
) -> list[Ball]:
    """Up to `count` balls, one per corridor column, left to right: the best
    candidates at quarter resolution, each confirmed at CONFIRM_SCORE where it
    was found.
    """
    template = ring_template(radius_px, _DETECT_SCALE)
    half = template.shape[0] // 2
    scores = cv2.matchTemplate(
        _downsample(frame, _DETECT_SCALE).astype(np.float32),
        template,
        cv2.TM_CCOEFF_NORMED,
    )
    column = int(
        np.ceil(3 * radius_px / _DETECT_SCALE)
    )  # a corridor's half-width and more
    offset = half + (_DETECT_SCALE - 1) / (2 * _DETECT_SCALE)
    balls = []
    while len(balls) < count:
        x, y, score = _peak(scores)
        if score < DETECT_SCORE:
            break
        scores[:, max(0, round(x) - column) : round(x) + column + 1] = -1.0
        lane = Lane(
            (x + offset) * _DETECT_SCALE, (y + offset) * _DETECT_SCALE, radius_px
        )
        ball = lane.update(frame)
        if ball is not None and ball.score >= CONFIRM_SCORE:
            balls.append(ball)
    return sorted(balls, key=lambda b: b.x)


class Lane:
    """One corridor's ball, in a column `2 x half_width` px wide centered on
    `x`: searched within `search_px` of its last position (a ball moves at
    most ~15 px a frame at 80 fps; the fly beside it is further), the whole column
    (`y_range`, default the frame) when that match is weak.
    """

    def __init__(
        self,
        x: float,
        y: float,
        radius_px: float = BALL_RADIUS_PX,
        *,
        half_width: float | None = None,
        search_px: float | None = None,
        y_range: tuple[int, int] | None = None,
    ):
        self.radius_px = radius_px
        half = round(half_width if half_width is not None else 2.2 * radius_px)
        self.x0 = max(0, round(x) - half)
        self.x1 = round(x) + half
        self.search_px = search_px if search_px is not None else 0.8 * radius_px
        self.y_range = y_range
        self.template = ring_template(radius_px, _TRACK_SCALE)
        self.ball = Ball(x, y, 1.0)  # the last position found
        self.lost = False

    def update(self, frame: np.ndarray) -> Ball | None:
        """The ball in `frame`, or None while it cannot be found (its last
        position is kept).
        """
        reach = 1.35 * self.radius_px + self.search_px
        near = self._match(frame, self.ball.y - reach, self.ball.y + reach)
        found = near if near.score >= TRACK_SCORE else None
        if found is None:
            y0, y1 = self.y_range or (0, frame.shape[0])
            anywhere = self._match(frame, y0, y1)
            if anywhere.score >= FIND_SCORE:
                found = anywhere
            elif near.score >= KEEP_SCORE:
                found = near
        self.lost = found is None
        if found is not None:
            self.ball = found
        return found

    def _match(self, frame: np.ndarray, y0: float, y1: float) -> Ball:
        lo = max(0, int(y0))
        lo -= lo % _TRACK_SCALE
        hi = min(frame.shape[0], int(np.ceil(y1)))
        roi = frame[lo:hi, self.x0 : self.x1]
        small = _downsample(roi, _TRACK_SCALE)
        if (
            small.shape[0] < self.template.shape[0]
            or small.shape[1] < self.template.shape[1]
        ):
            return Ball(self.ball.x, self.ball.y, -1.0)
        scores = cv2.matchTemplate(
            small.astype(np.float32), self.template, cv2.TM_CCOEFF_NORMED
        )
        x, y, score = _peak(scores)
        half = self.template.shape[0] // 2
        offset = (_TRACK_SCALE - 1) / 2
        return Ball(
            self.x0 + (x + half) * _TRACK_SCALE + offset,
            lo + (y + half) * _TRACK_SCALE + offset,
            score,
        )
