"""Garde-fous d'usage : limite de questions par visiteur et budget quotidien.

L'adresse IP n'est gardée qu'en mémoire, sous forme d'empreinte, le temps de la fenêtre.
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque


class Limiteur:
    def __init__(self, maximum: int, fenetre_s: int):
        self.maximum, self.fenetre = maximum, fenetre_s
        self._appels: dict[str, deque] = defaultdict(deque)
        self._verrou = threading.Lock()

    def autoriser(self, ip: str) -> tuple[bool, int]:
        """Renvoie (autorisé, secondes avant la prochaine question possible)."""
        cle = hashlib.sha256(ip.encode()).hexdigest()[:16]
        maintenant = time.monotonic()
        with self._verrou:
            q = self._appels[cle]
            while q and maintenant - q[0] > self.fenetre:
                q.popleft()
            if len(q) >= self.maximum:
                return False, int(self.fenetre - (maintenant - q[0])) + 1
            q.append(maintenant)
            if len(self._appels) > 10_000:  # nettoyage des visiteurs inactifs
                for k in [k for k, v in self._appels.items() if not v]:
                    del self._appels[k]
            return True, 0
