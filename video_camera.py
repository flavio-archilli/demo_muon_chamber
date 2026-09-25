"""
Visualizzatore video dedicato per la dimostrazione con la camera a muoni.

Finestra separata dal controllo HV: mostra il flusso della camera USB
(endoscopio) con snapshot, registrazione e modalita' schermo intero.

Avvio:
    /Users/farchill/.venv/hep2/bin/python3 video_camera.py

Scorciatoie:
    F o F11   schermo intero on/off
    Esc       esci dallo schermo intero
    S         salva uno snapshot
    Spazio    avvia/ferma la registrazione
"""

import datetime
import pathlib
import tkinter as tk
from tkinter import messagebox

import cv2
import customtkinter as ctk
from PIL import Image, ImageTk

import camera_backend as cam

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

CARTELLA_USCITA = pathlib.Path(__file__).parent / "acquisizioni"
AUTO = "Auto (prima con immagine)"
NESSUNA = "(nessuna)"
INTERVALLO_MS = 33  # ~30 fps di aggiornamento dello schermo


class VideoApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Camera a muoni - Monitor video")
        self.geometry("1000x760")
        self.minsize(720, 560)

        self.stream: cam.CameraStream | None = None
        self.writer: cv2.VideoWriter | None = None
        self.file_video: pathlib.Path | None = None
        self.schermo_intero = False
        self.specchia = False
        self.rotazione = 0  # 0 / 90 / 180 / 270
        self._photo = None  # riferimento vivo, altrimenti Tk scarta l'immagine

        # La sonda apre le camere a una a una e costa qualche secondo: si fa
        # dopo aver mostrato la finestra, altrimenti all'avvio sembra bloccata.
        self.camere: list[cam.CameraTrovata] = []

        self._costruisci_barra()
        self._costruisci_video()
        self._costruisci_stato()

        for sequenza, azione in (
            ("<f>", self.toggle_fullscreen), ("<F11>", self.toggle_fullscreen),
            ("<Escape>", self.esci_fullscreen),
            ("<s>", lambda _e: self.snapshot()),
            ("<space>", lambda _e: self.toggle_registrazione()),
        ):
            self.bind(sequenza, azione)

        self.after(200, self.rileva_camere)
        self.aggiorna_video()

    # ----------------------------------------------------------- costruzione
    def _costruisci_barra(self):
        barra = ctk.CTkFrame(self)
        barra.pack(side="top", fill="x", padx=10, pady=(10, 6))

        ctk.CTkLabel(barra, text="Camera:").pack(side="left", padx=(10, 6), pady=10)
        self.menu_camera = ctk.CTkOptionMenu(barra, values=self._etichette_camere(), width=230)
        self.menu_camera.set(AUTO)
        self.menu_camera.pack(side="left", padx=6, pady=10)

        ctk.CTkLabel(barra, text="Risoluzione:").pack(side="left", padx=(16, 6))
        self.menu_risoluzione = ctk.CTkOptionMenu(
            barra, values=[f"{w}x{h}" for w, h in cam.RISOLUZIONI], width=120)
        self.menu_risoluzione.set("1280x720")
        self.menu_risoluzione.pack(side="left", padx=6)

        self.btn_avvia = ctk.CTkButton(barra, text="Avvia", width=90,
                                       command=self.toggle_stream)
        self.btn_avvia.pack(side="left", padx=(16, 6))

        self.btn_rescan = ctk.CTkButton(barra, text="↻", width=36,
                                        command=self.rileva_camere)
        self.btn_rescan.pack(side="left", padx=(0, 10))

    def _costruisci_video(self):
        # Sfondo nero: in sala buia durante la dimostrazione e' meno fastidioso.
        self.area_video = tk.Label(self, bg="black", bd=0, highlightthickness=0)
        self.area_video.pack(side="top", fill="both", expand=True, padx=10, pady=6)

        comandi = ctk.CTkFrame(self)
        comandi.pack(side="top", fill="x", padx=10, pady=6)

        self.btn_snapshot = ctk.CTkButton(comandi, text="Snapshot (S)", width=130,
                                          state="disabled", command=self.snapshot)
        self.btn_snapshot.pack(side="left", padx=10, pady=10)

        self.btn_rec = ctk.CTkButton(comandi, text="● Registra (Spazio)", width=170,
                                     fg_color="#a03030", hover_color="#802020",
                                     state="disabled", command=self.toggle_registrazione)
        self.btn_rec.pack(side="left", padx=6, pady=10)

        self.btn_specchia = ctk.CTkButton(comandi, text="Specchia", width=100,
                                          fg_color="gray40", hover_color="gray30",
                                          command=self.toggle_specchia)
        self.btn_specchia.pack(side="left", padx=(20, 6), pady=10)

        self.btn_ruota = ctk.CTkButton(comandi, text="Ruota 90°", width=100,
                                       fg_color="gray40", hover_color="gray30",
                                       command=self.ruota)
        self.btn_ruota.pack(side="left", padx=6, pady=10)

        self.btn_fullscreen = ctk.CTkButton(comandi, text="Schermo intero (F)", width=160,
                                            fg_color="gray40", hover_color="gray30",
                                            command=self.toggle_fullscreen)
        self.btn_fullscreen.pack(side="right", padx=10, pady=10)

    def _costruisci_stato(self):
        self.lbl_stato = ctk.CTkLabel(self, text="Pronto", font=ctk.CTkFont(size=11),
                                      text_color="gray")
        self.lbl_stato.pack(side="bottom", pady=(0, 10))

    def _avvisa_nessuna_camera(self):
        if cam.permesso_negato():
            messagebox.showwarning(
                "Permesso fotocamera",
                "Il sistema vede una camera ma non e' autorizzata.\n\n"
                "Apri Impostazioni di Sistema > Privacy e sicurezza > Fotocamera "
                "e abilita l'applicazione da cui lanci questo script "
                "(Terminale o Visual Studio Code), poi riavviala.",
            )
        else:
            messagebox.showwarning(
                "Nessuna camera",
                "Nessuna camera rilevata. Collega l'endoscopio USB e premi ↻.",
            )

    # -------------------------------------------------------------- utilita'
    def set_stato(self, testo: str):
        self.lbl_stato.configure(text=testo)

    def _etichette_camere(self) -> list[str]:
        return [AUTO] + [t.etichetta() for t in self.camere]

    def _indice_scelto(self) -> int | None:
        """
        Indice OpenCV della camera da aprire.

        In automatico prende la prima che consegna un'immagine non nera: e' il
        comportamento giusto qui, perche' gli indici non sono stabili e i nomi
        non corrispondono agli indici (vedi camera_backend.nomi_camere).
        """
        scelta = self.menu_camera.get()
        if scelta in (AUTO, NESSUNA):
            self.set_stato("Ricerca della camera con immagine...")
            self.update_idletasks()
            return cam.prima_camera_utile()
        return int(scelta.split(":", 1)[0])

    def rileva_camere(self):
        self.set_stato("Ricerca camere in corso...")
        self.update_idletasks()
        self.camere = cam.sonda_camere()
        self.menu_camera.configure(values=self._etichette_camere())
        self.menu_camera.set(AUTO)
        if not self.camere:
            self.set_stato("Nessuna camera trovata")
            self._avvisa_nessuna_camera()
            return
        utili = sum(1 for t in self.camere if not t.nera)
        self.set_stato(f"{len(self.camere)} camere trovate, {utili} con immagine — "
                       f"premi Avvia")

    # ---------------------------------------------------------------- stream
    def toggle_stream(self):
        if self.stream is None:
            self.avvia()
        else:
            self.ferma()

    def avvia(self):
        indice = self._indice_scelto()
        if indice is None:
            self._avvisa_nessuna_camera()
            return

        larghezza, altezza = (int(v) for v in self.menu_risoluzione.get().split("x"))
        try:
            self.stream = cam.CameraStream(indice, larghezza, altezza)
        except cam.CameraError as exc:
            messagebox.showerror("Errore camera", str(exc))
            self.stream = None
            return

        self.btn_avvia.configure(text="Ferma")
        self.menu_camera.configure(state="disabled")
        self.menu_risoluzione.configure(state="disabled")
        self.btn_rescan.configure(state="disabled")
        self.btn_snapshot.configure(state="normal")
        self.btn_rec.configure(state="normal")
        w, h = self.stream.risoluzione
        chiesta = self.menu_risoluzione.get()
        nota = "" if chiesta == f"{w}x{h}" else f"  (chiesta {chiesta}, non supportata)"
        self.set_stato(f"In diretta — camera {indice} — {w}x{h}{nota}")

    def ferma(self):
        if self.writer is not None:
            self.toggle_registrazione()
        if self.stream is not None:
            self.stream.close()
            self.stream = None

        self.btn_avvia.configure(text="Avvia")
        self.menu_camera.configure(state="normal")
        self.menu_risoluzione.configure(state="normal")
        self.btn_rescan.configure(state="normal")
        self.btn_snapshot.configure(state="disabled")
        self.btn_rec.configure(state="disabled")
        self.area_video.configure(image="")
        self._photo = None
        self.set_stato("Fermo")

    # ------------------------------------------------------- trasformazioni
    def toggle_specchia(self):
        self.specchia = not self.specchia
        self.btn_specchia.configure(fg_color="#1f6aa5" if self.specchia else "gray40")

    def ruota(self):
        self.rotazione = (self.rotazione + 90) % 360
        self.btn_ruota.configure(text=f"Rotazione {self.rotazione}°")

    def _trasforma(self, frame):
        if self.specchia:
            frame = cv2.flip(frame, 1)
        if self.rotazione == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif self.rotazione == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif self.rotazione == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame

    # ------------------------------------------------------------- acquisizioni
    def snapshot(self):
        if self.stream is None:
            return
        frame = self.stream.frame()
        if frame is None:
            return
        CARTELLA_USCITA.mkdir(exist_ok=True)
        nome = datetime.datetime.now().strftime("snapshot_%Y%m%d_%H%M%S.png")
        percorso = CARTELLA_USCITA / nome
        cv2.imwrite(str(percorso), self._trasforma(frame))
        self.set_stato(f"Snapshot salvato: {percorso.name}")

    def toggle_registrazione(self):
        if self.stream is None:
            return

        if self.writer is None:
            frame = self.stream.frame()
            if frame is None:
                return
            frame = self._trasforma(frame)
            h, w = frame.shape[:2]
            CARTELLA_USCITA.mkdir(exist_ok=True)
            nome = datetime.datetime.now().strftime("video_%Y%m%d_%H%M%S.mp4")
            self.file_video = CARTELLA_USCITA / nome
            fps = self.stream.fps or 25.0
            self.writer = cv2.VideoWriter(
                str(self.file_video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
            if not self.writer.isOpened():
                self.writer = None
                messagebox.showerror("Errore", "Impossibile aprire il file video.")
                return
            self.btn_rec.configure(text="■ Ferma registrazione", fg_color="#d04040")
        else:
            self.writer.release()
            self.writer = None
            self.btn_rec.configure(text="● Registra (Spazio)", fg_color="#a03030")
            self.set_stato(f"Registrazione salvata: {self.file_video.name}")

    # ------------------------------------------------------------ fullscreen
    def toggle_fullscreen(self, _evento=None):
        self.schermo_intero = not self.schermo_intero
        self.attributes("-fullscreen", self.schermo_intero)

    def esci_fullscreen(self, _evento=None):
        if self.schermo_intero:
            self.schermo_intero = False
            self.attributes("-fullscreen", False)

    # --------------------------------------------------------------- disegno
    def aggiorna_video(self):
        if self.stream is not None:
            if not self.stream.attivo:
                errore = self.stream.errore or "flusso terminato"
                self.ferma()
                self.set_stato(f"Errore: {errore}")
            else:
                frame = self.stream.frame()
                if frame is not None:
                    frame = self._trasforma(frame)
                    if self.writer is not None:
                        self.writer.write(frame)
                    self._mostra(frame)

        self.after(INTERVALLO_MS, self.aggiorna_video)

    def _mostra(self, frame):
        disponibile_w = max(self.area_video.winfo_width(), 1)
        disponibile_h = max(self.area_video.winfo_height(), 1)
        if disponibile_w < 10 or disponibile_h < 10:
            return  # finestra non ancora disegnata

        h, w = frame.shape[:2]
        scala = min(disponibile_w / w, disponibile_h / h)
        nuove = (max(int(w * scala), 1), max(int(h * scala), 1))

        immagine = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        immagine = immagine.resize(nuove, Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(immagine)
        self.area_video.configure(image=self._photo)

        stato = f"In diretta — {w}x{h} — {self.stream.fps:.1f} fps"
        if self.writer is not None:
            stato += f"  ●  REC → {self.file_video.name}"
        if self.stream.frame_nero():
            stato += "  ⚠ immagine tutta nera: accendi i LED (rotella sul cavo) o togli il cappuccio"
        self.set_stato(stato)


def main():
    app = VideoApp()

    def alla_chiusura():
        app.ferma()
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", alla_chiusura)
    app.mainloop()


if __name__ == "__main__":
    main()
