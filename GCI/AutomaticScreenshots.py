import keyboard
import time
import os
from datetime import datetime
from PIL import ImageGrab
import win32com.client
import pythoncom

# ========= CONFIG =========

HOTKEY = "alt+s"

DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
WORD_PATH = os.path.join(DOWNLOADS, "screenshots.docx")

SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080

CUT_TOP = 120      # cortar arriba
CUT_BOTTOM = 50    # 🔥 ahora más largo hacia abajo

# ===========================

def take_screenshot():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temp_path = os.path.join(DOWNLOADS, f"temp_{timestamp}.png")

    img = ImageGrab.grab(bbox=(
        0,
        CUT_TOP,
        SCREEN_WIDTH,
        SCREEN_HEIGHT - CUT_BOTTOM
    ))

    img.save(temp_path)
    return temp_path


def get_word():
    try:
        word = win32com.client.GetActiveObject("Word.Application")
    except:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = True
    return word


def get_document(word):
    for doc in word.Documents:
        if doc.FullName.lower() == WORD_PATH.lower():
            return doc

    if os.path.exists(WORD_PATH):
        return word.Documents.Open(WORD_PATH)
    else:
        doc = word.Documents.Add()
        doc.SaveAs(WORD_PATH)
        return doc


def add_to_word(image_path):
    pythoncom.CoInitialize()

    try:
        word = get_word()
        doc = get_document(word)

        # Ir al final
        rng = doc.Range(doc.Content.End - 1)
        rng.InsertParagraphAfter()
        rng = doc.Range(doc.Content.End - 1)

        rng.InlineShapes.AddPicture(image_path)

        doc.Save()

    finally:
        pythoncom.CoUninitialize()

    os.remove(image_path)


def on_hotkey():
    time.sleep(0.2)
    image_path = take_screenshot()
    add_to_word(image_path)
    print("📸 Screenshot agregado correctamente.")


def main():
    print("ALT+S para screenshot. Ctrl+C para salir.")
    keyboard.add_hotkey(HOTKEY, on_hotkey)

    keyboard.wait()


if __name__ == "__main__":
    main()
