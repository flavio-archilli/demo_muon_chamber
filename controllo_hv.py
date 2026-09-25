"""
Controllo da riga di comando di un alimentatore HV CAEN DT547x.

Esempi:
    python controllo_hv.py --porte                      # elenca le porte seriali
    python controllo_hv.py --info                       # identifica il modulo
    python controllo_hv.py --vset 500                   # rampa a 500 V
    python controllo_hv.py --port /dev/cu.usbserial-1234 --vset 500
    python controllo_hv.py --backend sim --vset 500     # senza hardware
"""

import argparse
import sys
import time

import hv_backend as backend


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Controllo alimentatore HV CAEN DT547x")
    p.add_argument("--backend", choices=backend.BACKENDS, default=backend.BACKEND_DT54XX,
                   help="dt54xx = seriale (default), hvwrapper = crate CAEN, sim = simulatore")
    p.add_argument("--port", default=None,
                   help="porta seriale; se omessa viene scelta la prima disponibile")
    p.add_argument("--rtscts", action="store_true",
                   help="abilita il flow control hardware sulla seriale")
    p.add_argument("--porte", action="store_true",
                   help="elenca le porte seriali disponibili ed esce")
    p.add_argument("--info", action="store_true",
                   help="stampa identificativo e stato del modulo ed esce")
    p.add_argument("--vset", type=float, default=None,
                   help="tensione da impostare in volt; senza questa opzione non accende l'HV")
    p.add_argument("--iset", type=float, default=None, help="limite di corrente in uA")
    p.add_argument("--rup", type=float, default=None, help="rampa di salita in V/s")
    p.add_argument("--rdw", type=float, default=None, help="rampa di discesa in V/s")
    p.add_argument("--durata", type=int, default=10,
                   help="secondi di monitoraggio durante la rampa (default: 10)")
    p.add_argument("--lascia-acceso", action="store_true",
                   help="NON spegne l'HV all'uscita (default: spegnimento di sicurezza)")
    # Opzioni del backend HV Wrapper
    p.add_argument("--system", default=backend.DEFAULT_SYSTEM_TYPE)
    p.add_argument("--link", default=backend.DEFAULT_LINK_TYPE)
    p.add_argument("--arg", default=backend.DEFAULT_ARG)
    p.add_argument("--slot", type=int, default=backend.DEFAULT_SLOT)
    p.add_argument("--canale", type=int, default=backend.DEFAULT_CHANNEL)
    return p.parse_args(argv)


def apri(args):
    return backend.open_device(
        backend=args.backend,
        port=args.port,
        rtscts=args.rtscts,
        arg=args.arg,
        system_type=args.system,
        link_type=args.link,
        slot=args.slot,
        channel=args.canale,
    )


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.porte:
        porte = backend.list_serial_ports()
        if porte:
            print("Porte seriali disponibili:")
            for porta in porte:
                print("   ", porta)
        else:
            print("Nessuna porta seriale trovata.")
            if not backend.is_serial_available():
                print("pyserial non installato:", backend.serial_error(), file=sys.stderr)
        return 0

    try:
        device = apri(args)
    except Exception as exc:
        print(f"ERRORE di connessione: {exc}", file=sys.stderr)
        if args.backend == backend.BACKEND_DT54XX:
            print("        Prova --porte per vedere le porte disponibili,", file=sys.stderr)
            print("        oppure --backend sim per lavorare senza hardware.", file=sys.stderr)
        return 1

    acceso_da_noi = False

    try:
        nome = device.info()
        print(f"Connesso a: {nome}" if nome else "Connesso.")

        if args.info:
            print("Parametri:", ", ".join(device.parameters()))
            try:
                print(f"VSet: {device.read('VSet')} V | VMon: {device.vmon:.1f} V "
                      f"| IMon: {device.imon:.2f} uA")
                print("Stato:", backend.describe_status(device.status))
            except Exception as exc:
                print(f"ATTENZIONE: lettura parziale: {exc}", file=sys.stderr)
            return 0

        for nome_par, valore in (("RUp", args.rup), ("RDWn", args.rdw), ("ISet", args.iset)):
            if valore is not None:
                device.write(nome_par, valore)
                print(f"{nome_par} impostato a {valore}")

        if args.vset is None:
            print(f"VMon attuale: {device.vmon:.1f} V | IMon: {device.imon:.2f} uA")
            print("Stato:", backend.describe_status(device.status))
            print("(nessun --vset indicato: HV non acceso)")
            return 0

        device.write("VSet", args.vset)
        print(f"Target impostato a: {args.vset} V")

        device.power(True)
        acceso_da_noi = True
        print("Interruttore HV: ON. Inizio rampa...")

        for _ in range(args.durata):
            print(f"Monitor -> V: {device.vmon:7.1f} V | I: {device.imon:6.2f} uA "
                  f"| {backend.describe_status(device.status)}")
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
    except Exception as exc:
        print(f"Si e' verificato un errore durante il controllo: {exc}", file=sys.stderr)
    finally:
        # Spegne solo se l'HV e' stato acceso da questa esecuzione: cosi' una
        # semplice lettura (--info, o senza --vset) non spegne un canale che
        # stava gia' lavorando.
        if not acceso_da_noi:
            pass
        elif args.lascia_acceso:
            print("HV lasciato acceso su richiesta (--lascia-acceso).")
        else:
            print("Spegnimento di sicurezza (HV OFF)...")
            try:
                device.power(False)
            except Exception as exc:
                print(f"ATTENZIONE: spegnimento fallito: {exc}", file=sys.stderr)
        try:
            device.close()
        except Exception as exc:
            print(f"ATTENZIONE: chiusura fallita: {exc}", file=sys.stderr)
        print("Sessione chiusa.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
