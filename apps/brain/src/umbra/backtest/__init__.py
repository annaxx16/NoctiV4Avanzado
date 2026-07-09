"""Framework de backtesting y validación offline (Bloque A de la v2).

Todo el módulo es lógica pura (sin DB ni red): se alimenta de snapshots y
outcomes ya cargados y produce trades + métricas. Esto permite tests rápidos
y sin infra, y reproducibilidad total.
"""
