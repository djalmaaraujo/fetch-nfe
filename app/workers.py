# -*- coding: utf-8 -*-
"""Processos de fundo, num único container:

- Scheduler: a cada INTERVALO_SEGUNDOS sincroniza TODAS as empresas ativas, em
  paralelo (SYNC_WORKERS threads — o rate da SEFAZ é por certificado/CNPJ, então
  empresas diferentes podem andar juntas; dentro de uma empresa é serial + lock).
- Fila: a cada FILA_INTERVALO_SEGUNDOS consome a fila de manifestação, uma thread
  por empresa (FILA_WORKERS), serial dentro da empresa.

INTERVALO_SEGUNDOS=0 => roda UMA sincronização de tudo, drena a fila e sai.
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import config, empresas, nfe, store


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
            drenar_fila()
        except Exception:
            nfe.log("Erro no worker da fila:\n" + traceback.format_exc())
        time.sleep(config.FILA_INTERVALO)


def main() -> None:
    store.init()
    empresas.seed_do_env()
    store.resetar_processando_orfaos()

    if config.INTERVALO <= 0:  # execução única
        sincronizar_todas()
        drenar_fila()
        return

    threading.Thread(target=_loop_fila, daemon=True, name="fila").start()
    while True:
        try:
            sincronizar_todas()
        except Exception:
            nfe.log("Erro no ciclo do scheduler:\n" + traceback.format_exc())
        nfe.log(f"Scheduler: aguardando {config.INTERVALO}s até o próximo ciclo.")
        time.sleep(config.INTERVALO)


if __name__ == "__main__":
    main()
