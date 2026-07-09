# Edge 06 — Liquidity Vacuum (Vacío de Liquidez)

> **Estado**: NO IMPLEMENTADO. Requiere CLOB API (Bloque B, GAP-03).

---

## Hipótesis

En mercados de baja profundidad, una orden relativamente pequeña puede mover el precio de forma desproporcionada. Este over-shoot no refleja nueva información, sino simplemente la absorción de liquidez disponible en el libro. Una vez que la orden se ejecuta, el precio tiende a revertir parcialmente porque el desequilibrio era temporal, no informado.

El edge: detectar ese impacto en el libro y entrar **en la dirección contraria** antes de la reversión.

---

## Señal Matemática

```
orderbook_depth  = Σ(tamaños en los 5 mejores niveles de bids + asks) en USD
price_impact     = |ΔP en últimos 2 snapshots|  en puntos de probabilidad

Vacuum = price_impact / orderbook_depth   (en pp / USD)

Si Vacuum > θ₆ (calibrar; ej. 0.001 pp por USD de profundidad):
  Y el movimiento NO está acompañado de aumento de volumen externo:
    → señal de reversión en dirección contraria al spike
```

**Interpretación**: si el precio saltó 5pp con solo $500 de profundidad en el libro (Vacuum = 0.01), es un over-shoot por liquidez. Si saltó 5pp con $50,000 de profundidad (Vacuum = 0.0001), el movimiento probablemente tiene información detrás.

---

## Features Requeridos

| Feature | Fuente | Requiere |
|---|---|---|
| `orderbook_depth` | CLOB API: `GET /book?token_id=...` | CLOB API (GAP-03) |
| `bid_ask_imbalance` | `(bid_depth - ask_depth) / total_depth` | CLOB API |
| `price_impact` | `|mid(t) - mid(t-2snaps)|` | Ya calculable con snapshots |
| `vol_z` | `features/calculator.py` | Ya implementado |
| `spread` | `features/calculator.py` | Ya implementado |

---

## Parámetros Configurables

| Parámetro | Valor sugerido | Descripción |
|---|---|---|
| `vacuum_threshold` | 0.0005 | pp por USD de profundidad |
| `vacuum_min_price_impact` | 0.02 | Solo señal si el spike fue ≥2pp |
| `vacuum_max_vol_z` | 1.5 | Si vol_z > 1.5, el movimiento puede estar informado |
| `vacuum_depth_min_usd` | 500 | No operar si la profundidad total es < $500 (demasiado ilíquido) |

---

## Pseudocódigo

```python
@dataclass
class CLOBSnapshot:
    bids: list[tuple[float, float]]  # (precio, tamaño_USD)
    asks: list[tuple[float, float]]
    ts: datetime

def detect_liquidity_vacuum(
    snapshots: list[SnapshotInput],
    clob_snapshot: CLOBSnapshot,
    features: FeatureSet,
    as_of: datetime,
    vacuum_threshold: float = 0.0005,
    min_price_impact: float = 0.02,
    max_vol_z: float = 1.5,
) -> EdgeOutput | None:
    
    # Calcular profundidad del libro
    depth_bids = sum(size for _, size in clob_snapshot.bids[:5])
    depth_asks = sum(size for _, size in clob_snapshot.asks[:5])
    orderbook_depth = depth_bids + depth_asks
    
    if orderbook_depth < 500:
        return None  # libro demasiado vacío; riesgo de manipulación
    
    # Calcular price impact reciente
    history = [s for s in snapshots if s.ts <= as_of]
    mids = [mid(s) for s in history if mid(s) is not None]
    if len(mids) < 3:
        return None
    
    price_impact = abs(mids[-1] - mids[-3])  # últimos ~60 segundos
    if price_impact < min_price_impact:
        return None  # movimiento pequeño; no hay vacuum
    
    vacuum = price_impact / orderbook_depth
    if vacuum < vacuum_threshold:
        return None
    
    # Confirmar que no hay información detrás (vol_z bajo)
    if features.vol_z is not None and features.vol_z > max_vol_z:
        return None  # puede haber información real; no operar
    
    # Dirección: ir en contra del spike
    direction = mids[-1] - mids[-3]
    side = "BUY_NO" if direction > 0 else "BUY_YES"
    fair_price = mids[-3]  # precio "justo" antes del impact
    
    return EdgeOutput(
        edge_name="liquidity_vacuum",
        side=side,
        market_price=mids[-1],
        fair_price=fair_price,
        edge_value=price_impact,
        strength=vacuum,
        reason=f"vacuum={vacuum:.6f} depth={orderbook_depth:.0f}USD impact={price_impact:.4f}",
    )
```

---

## Integración con CLOB API

```python
class CLOBClient:
    BASE = "https://clob.polymarket.com"
    
    async def get_book(self, token_id: str) -> CLOBSnapshot:
        resp = await self.http.get(f"{self.BASE}/book", params={"token_id": token_id})
        data = resp.json()
        return CLOBSnapshot(
            bids=[(float(b["price"]), float(b["size"])) for b in data["bids"]],
            asks=[(float(a["price"]), float(a["size"])) for a in data["asks"]],
            ts=datetime.now(timezone.utc),
        )
```

---

## Criterios de Validación

| Métrica | Umbral |
|---|---|
| EV por señal | > 0.004 |
| Tiempo promedio de reversión | < 15 minutos (si > esto, el movimiento era informado) |
| % de reversiones observadas | > 55% |
| Brier score | < 0.24 |

**Riesgo principal**: si el vacuum es causado por un insider o información privada, la "reversión" no ocurre y se pierde la apuesta completa. El filtro de `vol_z` mitiga pero no elimina este riesgo.

---

## Pre-requisitos de Implementación

1. GAP-03 resuelto: `CLOBClient` con `get_book()` implementado.
2. `CLOBSnapshot` almacenado o consultado en tiempo real para cada evaluación.
3. Índice en `book_snapshots.market_id, ts DESC` para recuperar snapshots recientes rápidamente.
