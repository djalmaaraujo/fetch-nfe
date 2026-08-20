# -*- coding: utf-8 -*-
"""Processos de fundo, num único container:

- Scheduler: sincroniza TODAS as empresas ativas em paralelo (SYNC_WORKERS
  threads — o rate da SEFAZ é por certificado/CNPJ; dentro de uma empresa é
  serial + lock).
- Fila: consome a fila de manifestação, uma thread por empresa (FILA_WORKERS).

A rotina é configurável em RUNTIME via API (GET/PATCH /rotina): ativa/pausada e
intervalo ficam no banco e são relidos a cada tick (~15s) — sem restart.
Pausada = o worker não fala com a SEFAZ sozinho (nem sync, nem manifestação);
sync manual via POST /sincronizar continua funcionando.

INTERVALO_SEGUNDOS=0 no .env => roda UMA vez (sincroniza tudo + drena a fila) e sai.
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import config, empresas, extrator, nfe, store

TICK = 15  # segundos entre releituras da config da rotina


def sincronizar_todas() -> list:
    ativas = store.listar_empresas(somente_ativas=True)
    if not ativas:
        nfe.log("Nenhuma empresa ativa cadastrada.")
        return []
    with ThreadPoolExecutor(max_workers=config.SYNC_WORKERS) as pool:
        return list(pool.map(lambda e: nfe.sincronizar(e["cnpj"]), ativas))


def drenar_fila() -> list:
    por_empresa = store.reivindicar_pendentes()
    if not por_empresa:
        return []
    total = sum(len(v) for v in por_empresa.values())
    nfe.log(f"Fila: {total} manifestação(ões) em {len(por_empresa)} empresa(s).")
    with ThreadPoolExecutor(max_workers=config.FILA_WORKERS) as pool:
        return list(pool.map(
            lambda item: nfe.processar_fila_empresa(item[0], item[1]),
            por_empresa.items(),
        ))


def _loop_fila() -> None:
    while True:
        try:
            if store.rotina_config()["ativa"]:
                drenar_fila()
        except Exception:
            nfe.log("Erro no worker da fila:\n" + traceback.format_exc())
        time.sleep(config.FILA_INTERVALO)


def _loop_scheduler() -> None:
    avisou_pausa = False
    while True:
        try:
            cfg = store.rotina_config()
            if not cfg["ativa"]:
                if not avisou_pausa:
                    nfe.log("Rotina PAUSADA — sincronização automática suspensa.")
                    avisou_pausa = True
                time.sleep(TICK)
                continue
            if avisou_pausa:
                nfe.log("Rotina RETOMADA.")
                avisou_pausa = False

            ultima = float(store.get_estado("rotina_ultima_ts", "0") or 0)
            if time.time() >= ultima + cfg["intervalo_segundos"]:
                inicio = time.time()
                sincronizar_todas()
                store.registrar_execucao_rotina(inicio, time.time() - inicio)
                nfe.log(f"Rotina: próxima execução em ~{cfg['intervalo_segundos']}s.")
        except Exception:
            nfe.log("Erro no ciclo do scheduler:\n" + traceback.format_exc())
        time.sleep(TICK)


def main() -> None:
    store.init()
    empresas.seed_do_env()
    store.resetar_processando_orfaos()
    # Indexa notas antigas (colunas de busca vazias) sem segurar a subida
    threading.Thread(target=extrator.backfill, daemon=True, name="backfill").start()

    if config.INTERVALO <= 0:  # execução única
        sincronizar_todas()
        drenar_fila()
        return

    threading.Thread(target=_loop_fila, daemon=True, name="fila").start()
    _loop_scheduler()


if __name__ == "__main__":
    main()
