# -*- coding: utf-8 -*-
"""Rotina agendada: sincroniza a cada INTERVALO_SEGUNDOS (baixa as notas
recebidas até o momento). Use INTERVALO_SEGUNDOS=0 para rodar uma vez só."""
import time
import traceback

from . import config, nfe, store


def main() -> None:
    problemas = config.problemas()
    if problemas:
        nfe.log("Configuração incompleta: " + ", ".join(problemas))
        raise SystemExit(1)
    store.init()
    while True:
        try:
            nfe.sincronizar()
        except Exception:
            nfe.log("Erro no ciclo:\n" + traceback.format_exc())
        if config.INTERVALO <= 0:
            break
        nfe.log(f"Aguardando {config.INTERVALO}s até o próximo ciclo.")
        time.sleep(config.INTERVALO)


if __name__ == "__main__":
    main()
