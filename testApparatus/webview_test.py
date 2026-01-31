import webview
import tkinter as tk

root = tk.Tk()
width = root.winfo_screenwidth()
height = root.winfo_screenheight()
root.destroy()

print(f"Detected screen size: {width}x{height}")

webview.create_window("Test WebView", "https://example.com", width=width, height=height)
webview.start()
