"""
Pannello di controllo grafico per l'alimentatore CAEN DT547x (customtkinter).

Usa il backend seriale nativo (hv_backend.CaenDT54xxDevice): nessuna libreria
CAEN richiesta. Se non c'e' hardware collegato si puo' scegliere il backend
"sim" e lavorare col simulatore.
"""

from tkinter import messagebox

import customtkinter as ctk

import hv_backend as backend

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

INTERVALLO_MS = 1000


class CaenControlApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("CAEN DT547x - Pannello Controllo")
        self.geometry("520x760")
        self.resizable(False, False)

        self.device = None
        self.is_hv_on = False

        # --- TITOLO ---
        ctk.CTkLabel(self, text="CAEN DT547x Control",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(20, 2))
        self.lbl_modello = ctk.CTkLabel(self, text="modulo non connesso",
                                        font=ctk.CTkFont(size=12), text_color="gray")
        self.lbl_modello.pack(pady=(0, 12))

        # --- FRAME CONNESSIONE ---
        self.conn_frame = ctk.CTkFrame(self)
        self.conn_frame.pack(pady=8, padx=20, fill="x")
        self.conn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.conn_frame, text="Backend:").grid(
            row=0, column=0, padx=10, pady=(10, 5), sticky="w")
        self.backend_menu = ctk.CTkOptionMenu(
            self.conn_frame, values=list(backend.BACKENDS), command=self._cambia_backend)
        self.backend_menu.set(backend.BACKEND_DT54XX)
        self.backend_menu.grid(row=0, column=1, columnspan=2, padx=10, pady=(10, 5), sticky="ew")

        ctk.CTkLabel(self.conn_frame, text="Porta seriale:").grid(
            row=1, column=0, padx=10, pady=5, sticky="w")
        self.port_menu = ctk.CTkOptionMenu(self.conn_frame, values=["(nessuna)"])
        self.port_menu.grid(row=1, column=1, padx=10, pady=5, sticky="ew")
        self.btn_refresh = ctk.CTkButton(self.conn_frame, text="↻", width=36,
                                         command=self.aggiorna_porte)
        self.btn_refresh.grid(row=1, column=2, padx=(0, 10), pady=5)

        self.btn_connect = ctk.CTkButton(self.conn_frame, text="Connetti",
                                         command=self.toggle_connection)
        self.btn_connect.grid(row=2, column=0, columnspan=3, padx=10, pady=10, sticky="ew")
        # Colore di default, per ripristinarlo dopo la disconnessione.
        self._connect_color = self.btn_connect.cget("fg_color")
        self._connect_hover = self.btn_connect.cget("hover_color")

        # --- FRAME MONITORAGGIO ---
        self.monitor_frame = ctk.CTkFrame(self)
        self.monitor_frame.pack(pady=8, padx=20, fill="x")
        self.monitor_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self.monitor_frame, text="Tensione (VMon):",
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0, padx=15, pady=5, sticky="w")
        self.lbl_vmon = ctk.CTkLabel(self.monitor_frame, text="0.0 V",
                                     font=ctk.CTkFont(size=24, weight="bold"),
                                     text_color="#1f538d")
        self.lbl_vmon.grid(row=0, column=1, padx=15, pady=5, sticky="e")

        ctk.CTkLabel(self.monitor_frame, text="Corrente (IMon):",
                     font=ctk.CTkFont(size=12)).grid(row=1, column=0, padx=15, pady=5, sticky="w")
        self.lbl_imon = ctk.CTkLabel(self.monitor_frame, text="0.00 µA",
                                     font=ctk.CTkFont(size=24, weight="bold"),
                                     text_color="#1f538d")
        self.lbl_imon.grid(row=1, column=1, padx=15, pady=5, sticky="e")

        ctk.CTkLabel(self.monitor_frame, text="Stato canale:",
                     font=ctk.CTkFont(size=12)).grid(row=2, column=0, padx=15, pady=(5, 12), sticky="w")
        self.lbl_stato = ctk.CTkLabel(self.monitor_frame, text="—",
                                      font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_stato.grid(row=2, column=1, padx=15, pady=(5, 12), sticky="e")

        # --- FRAME COMANDI ---
        self.control_frame = ctk.CTkFrame(self)
        self.control_frame.pack(pady=8, padx=20, fill="x")
        self.control_frame.grid_columnconfigure(1, weight=1)

        self.campi = {}
        for riga, (etichetta, chiave, placeholder) in enumerate((
            ("Tensione VSet (V):", "VSet", "es. 500"),
            ("Limite ISet (µA):", "ISet", "es. 10"),
            ("Rampa salita (V/s):", "RUp", "es. 50"),
            ("Rampa discesa (V/s):", "RDWn", "es. 50"),
        )):
            ctk.CTkLabel(self.control_frame, text=etichetta).grid(
                row=riga, column=0, padx=10, pady=8, sticky="w")
            entry = ctk.CTkEntry(self.control_frame, placeholder_text=placeholder)
            entry.grid(row=riga, column=1, padx=10, pady=8, sticky="ew")
            bottone = ctk.CTkButton(self.control_frame, text="Invia", width=70, state="disabled",
                                    command=lambda k=chiave: self.invia_parametro(k))
            bottone.grid(row=riga, column=2, padx=10, pady=8)
            self.campi[chiave] = (entry, bottone)

        self.btn_hv = ctk.CTkButton(self.control_frame, text="HV ON", fg_color="green",
                                    hover_color="darkgreen", state="disabled",
                                    height=40, font=ctk.CTkFont(size=14, weight="bold"),
                                    command=self.toggle_hv)
        self.btn_hv.grid(row=4, column=0, columnspan=3, padx=10, pady=(15, 8), sticky="ew")

        self.btn_clear = ctk.CTkButton(self.control_frame, text="Clear alarm", state="disabled",
                                       fg_color="gray40", hover_color="gray30",
                                       command=self.clear_alarm)
        self.btn_clear.grid(row=5, column=0, columnspan=3, padx=10, pady=(0, 12), sticky="ew")

        # --- BARRA DI STATO ---
        self.status_label = ctk.CTkLabel(self, text="Non connesso",
                                         font=ctk.CTkFont(size=11), text_color="gray")
        self.status_label.pack(pady=(4, 14))

        self.aggiorna_porte()
        self.aggiorna_valori()

    # ------------------------------------------------------------------ utils
    @property
    def is_connected(self) -> bool:
        return self.device is not None

    def set_status(self, testo: str) -> None:
        self.status_label.configure(text=testo)

    def _cambia_backend(self, valore: str) -> None:
        seriale = valore == backend.BACKEND_DT54XX
        self.port_menu.configure(state="normal" if seriale else "disabled")
        self.btn_refresh.configure(state="normal" if seriale else "disabled")

    def aggiorna_porte(self):
        porte = backend.list_serial_ports()
        self.port_menu.configure(values=porte or ["(nessuna)"])
        self.port_menu.set(porte[0] if porte else "(nessuna)")
        if not porte:
            self.set_status("Nessuna porta seriale: collega il modulo, oppure usa il backend 'sim'")

    def _abilita_comandi(self, abilita: bool) -> None:
        stato = "normal" if abilita else "disabled"
        for _, bottone in self.campi.values():
            bottone.configure(state=stato)
        self.btn_hv.configure(state=stato)
        self.btn_clear.configure(state=stato)

    # ------------------------------------------------------------- connessione
    def toggle_connection(self):
        if not self.is_connected:
            self.connetti()
        else:
            self.disconnetti()

    def connetti(self):
        scelto = self.backend_menu.get()
        porta = self.port_menu.get()
        try:
            self.device = backend.open_device(
                backend=scelto,
                port=None if porta == "(nessuna)" else porta,
            )
            modello = self.device.info()
        except Exception as exc:
            messagebox.showerror("Errore connessione", f"{exc}")
            if self.device is not None:
                try:
                    self.device.close()
                except Exception:
                    pass
            self.device = None
            return

        self.lbl_modello.configure(text=modello or "modulo connesso",
                                   text_color="gray" if scelto != backend.BACKEND_SIM else "orange")
        self.btn_connect.configure(text="Disconnetti", fg_color="red", hover_color="darkred")
        self.backend_menu.configure(state="disabled")
        self.port_menu.configure(state="disabled")
        self.btn_refresh.configure(state="disabled")
        self._abilita_comandi(True)
        self._leggi_impostazioni_correnti()
        self.set_status("Connesso")

    def _leggi_impostazioni_correnti(self):
        """Precompila i campi con i valori gia' programmati nel modulo."""
        for chiave in self.campi:
            entry, _ = self.campi[chiave]
            try:
                valore = self.device.read(chiave)
            except Exception:
                continue
            entry.delete(0, "end")
            entry.insert(0, f"{float(valore):g}")
        try:
            self.is_hv_on = bool(self.device.status & 1)
        except Exception:
            self.is_hv_on = False
        self._aggiorna_bottone_hv()

    def disconnetti(self):
        if self.is_hv_on:
            self.toggle_hv()  # spegne l'HV prima di disconnettere, per sicurezza

        try:
            self.device.close()
        except Exception as exc:
            messagebox.showwarning("Attenzione", f"Errore in chiusura:\n{exc}")
        finally:
            self.device = None

        self.btn_connect.configure(text="Connetti", fg_color=self._connect_color,
                                   hover_color=self._connect_hover)
        self.backend_menu.configure(state="normal")
        self._cambia_backend(self.backend_menu.get())
        self._abilita_comandi(False)
        self.lbl_modello.configure(text="modulo non connesso", text_color="gray")
        self.lbl_vmon.configure(text="0.0 V")
        self.lbl_imon.configure(text="0.00 µA")
        self.lbl_stato.configure(text="—")
        self.set_status("Non connesso")

    # ----------------------------------------------------------------- comandi
    def invia_parametro(self, chiave: str):
        entry, _ = self.campi[chiave]
        try:
            valore = float(entry.get())
        except ValueError:
            messagebox.showwarning("Attenzione", f"Inserire un numero valido per {chiave}.")
            return

        try:
            self.device.write(chiave, valore)
        except Exception as exc:
            messagebox.showerror("Errore", f"Impossibile impostare {chiave}:\n{exc}")
            return

        self.set_status(f"{chiave} impostato a {valore:g}")

    def _aggiorna_bottone_hv(self):
        if self.is_hv_on:
            self.btn_hv.configure(text="HV OFF (SPEGNI)", fg_color="orange",
                                  hover_color="darkorange")
        else:
            self.btn_hv.configure(text="HV ON", fg_color="green", hover_color="darkgreen")

    def toggle_hv(self):
        nuovo_stato = not self.is_hv_on
        try:
            self.device.power(nuovo_stato)
        except Exception as exc:
            messagebox.showerror("Errore", f"Comando HV fallito:\n{exc}")
            return

        self.is_hv_on = nuovo_stato
        self._aggiorna_bottone_hv()
        self.set_status("HV ON" if self.is_hv_on else "HV OFF")

    def clear_alarm(self):
        try:
            self.device.clear_alarm()
        except Exception as exc:
            messagebox.showerror("Errore", f"Clear alarm fallito:\n{exc}")
            return
        self.set_status("Allarme azzerato")

    # ------------------------------------------------------------ monitoraggio
    def aggiorna_valori(self):
        """Legge ciclicamente i valori dal modulo, una volta al secondo."""
        if self.is_connected:
            try:
                self.lbl_vmon.configure(text=f"{self.device.vmon:.1f} V")
                self.lbl_imon.configure(text=f"{self.device.imon:.2f} µA")
                parola = self.device.status
                self.is_hv_on = bool(parola & 1)
                self.lbl_stato.configure(text=backend.describe_status(parola))
                self._aggiorna_bottone_hv()
            except Exception as exc:
                self.set_status(f"Errore lettura: {exc}")

        self.after(INTERVALLO_MS, self.aggiorna_valori)


def main():
    app = CaenControlApp()

    def on_closing():
        if app.is_connected:
            app.disconnetti()
        app.destroy()

    app.protocol("WM_DELETE_WINDOW", on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
