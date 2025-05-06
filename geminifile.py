import pyautogui
from pynput import keyboard
import tkinter as tk
from PIL import Image, ImageTk
import pytesseract
from pytesseract import Output
import requests
import base64
import io
import threading
import re
import time
import os 
from dotenv import load_dotenv
# Configuration

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") # Replace with your Gemini API key
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
HOTKEY = keyboard.Key.ctrl
EXIT_KEY = keyboard.Key.esc
CHECKMARK_IMAGE_PATH = "checkmark.png"  # Use PNG with transparency for best effect

current_keys = set()

def capture_screenshot():
    return pyautogui.screenshot()

def image_to_base64(img):
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

def get_answers_from_gemini(screenshot):
    """Get answers to all questions in the screenshot using Gemini API"""
    img_base64 = image_to_base64(screenshot)
    
    # Updated prompt to extract ALL questions and answers from image
    prompt = """
    Analyze this quiz screenshot. For EACH question in the image:
    1. Identify the question number or ID
    2. Extract the question text
    3. Determine the correct answer option - provide the EXACT text of the correct option

    Format your response as:
    Q1: [Question text]
    A1: [Correct answer text]
    
    Q2: [Question text]
    A2: [Correct answer text]
    
    And so on for all questions visible in the image.
    If there's only one question, use the Q1/A1 format.
    """
    
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": img_base64}}
            ]
        }]
    }
    
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    
    try:
        response = requests.post(GEMINI_API_URL, json=payload, headers=headers, params=params)
        response.raise_for_status()
        result = response.json()
        answer_text = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        
        # Parse the response to extract all answers
        answers = {}
        lines = answer_text.split("\n")
        current_q = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Question line
            q_match = re.match(r"Q(\d+):\s*(.*)", line)
            if q_match:
                current_q = q_match.group(1)
                answers[current_q] = {"question": q_match.group(2), "answer": None}
                continue
            
            # Answer line
            a_match = re.match(r"A(\d+):\s*(.*)", line)
            if a_match and current_q == a_match.group(1):
                answers[current_q]["answer"] = a_match.group(2)
        
        # If we couldn't parse structured format, try to extract just the answer
        if not answers:
            # Try to find direct answer (backward compatibility)
            answer_lines = [line for line in lines if line.strip()]
            if answer_lines:
                answers["1"] = {"question": "Question", "answer": answer_lines[0].strip()}
        
        return answers if answers else None
    except Exception as e:
        print(f"Error querying Gemini API: {e}")
        return None

def find_answer_positions(screenshot, answers):
    """Find screen positions for all answers"""
    # Convert screenshot to grayscale for better OCR results
    gray_screenshot = screenshot.convert('L')
    
    # Get OCR data with more aggressive configuration
    data = pytesseract.image_to_data(
        gray_screenshot, 
        output_type=Output.DICT,
        config='--psm 3 --oem 3'  # Page segmentation mode 3 (full page) and LSTM OCR Engine
    )
    
    # Create word positions from OCR data
    words = []
    for i, text in enumerate(data["text"]):
        if text.strip():
            words.append({
                "text": text.strip().lower(),
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
                "conf": data["conf"][i],
                "block_num": data["block_num"][i],
                "line_num": data["line_num"][i]
            })
    
    answer_positions = {}
    
    # For each answer, try to find its position
    for q_id, q_data in answers.items():
        answer_text = q_data["answer"].strip().lower()
        
        # Try to find exact match first
        found = False
        
        # Special case for option-style answers (A), B), etc.)
        option_match = re.match(r"^([A-Z])[)\.]?\s*(.*)", answer_text)
        if option_match:
            option_letter = option_match.group(1).lower()
            # Look for the option letter first
            for word in words:
                if word["text"].lower() == option_letter or word["text"].lower() == f"{option_letter})" or word["text"].lower() == f"{option_letter}.":
                    x = word["x"] + word["w"] // 2
                    y = word["y"] + word["h"] // 2
                    answer_positions[q_id] = {
                        "position": (x, y),
                        "found": True,
                        "text": q_data["answer"]
                    }
                    found = True
                    break
        
        # Direct exact match
        if not found:
            for word in words:
                if answer_text == word["text"].lower():
                    x = word["x"] + word["w"] // 2
                    y = word["y"] + word["h"] // 2
                    answer_positions[q_id] = {
                        "position": (x, y),
                        "found": True,
                        "text": q_data["answer"]
                    }
                    found = True
                    break
        
        # Try multi-word matching
        if not found:
            # Check for multi-word sequences
            answer_words = answer_text.split()
            if len(answer_words) > 1:
                # Group words by line and block
                lines = {}
                for word in words:
                    key = (word["block_num"], word["line_num"])
                    if key not in lines:
                        lines[key] = []
                    lines[key].append(word)
                
                # Sort words within each line by x position
                for key in lines:
                    lines[key].sort(key=lambda w: w["x"])
                
                # Try to match the sequence in each line
                for line_key, line_words in lines.items():
                    line_text = " ".join([w["text"] for w in line_words]).lower()
                    if answer_text in line_text:
                        # Find the approximate center of this answer in the line
                        start_idx = line_text.find(answer_text)
                        end_idx = start_idx + len(answer_text)
                        
                        # Find which words contain the start and end
                        char_count = 0
                        start_word_idx = end_word_idx = -1
                        for i, word in enumerate(line_words):
                            prev_char_count = char_count
                            char_count += len(word["text"]) + 1  # +1 for space
                            
                            if prev_char_count <= start_idx < char_count and start_word_idx == -1:
                                start_word_idx = i
                            
                            if prev_char_count < end_idx <= char_count:
                                end_word_idx = i
                                break
                        
                        if start_word_idx >= 0 and end_word_idx >= 0:
                            start_word = line_words[start_word_idx]
                            end_word = line_words[end_word_idx]
                            mid_x = (start_word["x"] + end_word["x"] + end_word["w"]) // 2
                            mid_y = (start_word["y"] + end_word["y"]) // 2
                            
                            answer_positions[q_id] = {
                                "position": (mid_x, mid_y),
                                "found": True,
                                "text": q_data["answer"]
                            }
                            found = True
                            break
        
        # Try to find key words if still not found
        if not found:
            # Try to find partial matches (word by word)
            answer_words = answer_text.split()
            for answer_word in answer_words:
                if len(answer_word) < 4:  # Skip short words
                    continue
                    
                for word in words:
                    if answer_word in word["text"].lower() and len(answer_word) > 3:
                        x = word["x"] + word["w"] // 2
                        y = word["y"] + word["h"] // 2
                        answer_positions[q_id] = {
                            "position": (x, y),
                            "found": True,
                            "text": q_data["answer"]
                        }
                        found = True
                        break
                
                if found:
                    break
        
        # If still not found, use default position
        if not found:
            default_x = 100
            default_y = 100 + (int(q_id) - 1) * 50  # Stack multiple answers vertically
            answer_positions[q_id] = {
                "position": (default_x, default_y),
                "found": False,
                "text": q_data["answer"]
            }
    
    return answer_positions

# Global root for Tkinter
root = None

def show_checkmarks(answer_positions):
    """Display checkmarks at all answer positions"""
    # Use the global root window
    global root
    
    # Load checkmark image
    try:
        checkmark_img = Image.open(CHECKMARK_IMAGE_PATH)
        checkmark_img = checkmark_img.resize((25, 25), Image.Resampling.LANCZOS)
    except Exception as e:
        print(f"Error loading checkmark image: {e}")
        # Create a simple checkmark as fallback
        checkmark_img = Image.new('RGBA', (25, 25), (0, 0, 0, 0))
    
    # Photo object to retain reference
    photo = ImageTk.PhotoImage(checkmark_img)
    
    checkmark_windows = []
    
    # Create a window for each answer
    for q_id, answer_data in answer_positions.items():
        position = answer_data["position"]
        found = answer_data["found"]
        text = answer_data["text"]
        
        win = tk.Toplevel(root)
        win.attributes("-topmost", True)
        win.overrideredirect(True)  # No window decorations
        
        if found:
            # Just show the checkmark
            canvas = tk.Canvas(win, width=25, height=25, highlightthickness=0, bg="white")
            canvas.pack()
            canvas.create_image(12, 12, image=photo)
            win.geometry(f"25x25+{position[0]-12}+{position[1]-12}")
        else:
            # Show checkmark with text
            frame = tk.Frame(win, bg="white", bd=1, relief=tk.SOLID)
            frame.pack()
            
            canvas = tk.Canvas(frame, width=25, height=25, highlightthickness=0, bg="white")
            canvas.grid(row=0, column=0, padx=2, pady=2)
            canvas.create_image(12, 12, image=photo)
            
            label = tk.Label(frame, text=text, font=("Arial", 12), bg="white", fg="black", wraplength=300)
            label.grid(row=0, column=1, padx=5, pady=2)
            
            win.geometry(f"+{position[0]}+{position[1]}")
        
        checkmark_windows.append(win)
    
    # Store the photo reference in the root window
    root.photo = photo
    
    # Close all windows after 5 seconds
    root.after(5000, lambda: [win.destroy() for win in checkmark_windows])

def process_screenshot():
    """Process screenshot and show answers"""
    try:
        print("Capturing screenshot...")
        screenshot = capture_screenshot()
        
        print("Querying Gemini API for answers...")
        answers = get_answers_from_gemini(screenshot)
        
        if not answers:
            print("Could not determine any answers.")
            return
            
        print(f"Found {len(answers)} question(s):")
        for q_id, q_data in answers.items():
            print(f"Q{q_id}: {q_data['answer']}")
        
        print("Finding answer positions...")
        answer_positions = find_answer_positions(screenshot, answers)
        
        print("Displaying checkmarks...")
        # Schedule the checkmark display on the main thread
        root.after(10, lambda: show_checkmarks(answer_positions))
        
    except Exception as e:
        print(f"Error processing screenshot: {e}")

def on_press(key):
    try:
        current_keys.add(key)
        if HOTKEY in current_keys and key == HOTKEY:
            # Schedule processing on the main thread
            threading.Thread(target=process_screenshot, daemon=True).start()
    except Exception as e:
        print(f"Error handling keypress: {e}")

def on_release(key):
    try:
        current_keys.discard(key)
        if key == EXIT_KEY:
            print("Exiting quiz overlay.")
            return False
    except Exception as e:
        print(f"Error handling key release: {e}")

def update_main_loop():
    """Function to keep the Tkinter main loop running while checking for keyboard events"""
    # Schedule next update
    root.after(100, update_main_loop)

def main():
    global root
    
    print(f"""
📚 Quiz Helper running.
- Press 'ctrl' to query answers for all questions in the screenshot
- Press 'esc' to quit
- Checkmarks will appear at answer positions
- If an answer can't be located, it will show at the side with text
""")
    # Check pytesseract installation
    try:
        pytesseract.get_tesseract_version()
    except Exception as e:
        print(f"⚠️ Tesseract not properly installed: {e}")
        print("Make sure Tesseract OCR is installed on your Linux system:")
        print("  sudo apt install tesseract-ocr")
        return
    
    # Initialize the Tkinter root window
    root = tk.Tk()
    root.withdraw()  # Hide the main window
    root.attributes("-topmost", True)
    
    # Start keyboard listener in a separate thread
    keyboard_thread = threading.Thread(
        target=lambda: keyboard.Listener(on_press=on_press, on_release=on_release).start(),
        daemon=True
    )
    keyboard_thread.start()
    
    # Set up the main loop update schedule
    update_main_loop()
    
    # Start Tkinter main loop
    try:
        root.mainloop()
    except KeyboardInterrupt:
        print("Exiting quiz helper.")
        pass

if __name__ == "__main__":
    main()