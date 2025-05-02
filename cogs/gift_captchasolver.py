#!/usr/bin/env python3
# Gift Captcha Solver for WOS Discord Bot

import os
import warnings
import base64
import asyncio
import time
import easyocr
import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from datetime import datetime

# Suppress PyTorch warnings
warnings.filterwarnings("ignore", message=".*pin_memory.*", category=UserWarning)

class GiftCaptchaSolver:
    def __init__(self, use_gpu=False, gpu_device=None, save_images=False):
        """
        Initialize the OCR captcha solver.
        
        Args:
            use_gpu (bool): Whether to use GPU for OCR.
            gpu_device (int, optional): GPU device ID to use.
            save_images (int): Image saving mode (0=None, 1=Failed, 2=Success, 3=All).
        """
        self.use_gpu = use_gpu
        self.gpu_device = gpu_device
        self.save_images_mode = save_images
        self.min_confidence = 0.4
        
        # Set up captcha image directory
        self.captcha_dir = 'captcha_images'
        os.makedirs(self.captcha_dir, exist_ok=True)
        
        # Initialize OCR based on GPU settings
        self._initialize_ocr()
        
        # OCR statistics
        self.stats = {
            "total_attempts": 0,
            "successful_decodes": 0, 
            "first_try_success": 0,
            "retries": 0,
            "failures": 0
        }
        self.reset_run_stats()
    
    def reset_run_stats(self):
        """Reset statistics for the current run."""
        self.run_stats = {
            "total_attempts": 0,
            "successful_decodes": 0, 
            "first_try_success": 0,
            "retries": 0,
            "failures": 0,
            "start_time": time.time()
        }
    
    def get_run_stats_report(self):
        """Get a formatted report of run statistics."""
        duration = time.time() - self.run_stats["start_time"]
        
        success_rate = 0
        if self.run_stats["total_attempts"] > 0:
            success_rate = (self.run_stats["successful_decodes"] / self.run_stats["total_attempts"]) * 100
            
        first_try_rate = 0
        if self.run_stats["successful_decodes"] > 0:
            first_try_rate = (self.run_stats["first_try_success"] / self.run_stats["successful_decodes"]) * 100
            
        avg_attempts = 0
        if self.run_stats["successful_decodes"] > 0:
            avg_attempts = (self.run_stats["total_attempts"] / self.run_stats["successful_decodes"])
        
        report = [
            "\n=== Captcha Solver Statistics ===",
            f"Total captcha attempts: {self.run_stats['total_attempts']}",
            f"Successful decodes: {self.run_stats['successful_decodes']}",
            f"First attempt success: {self.run_stats['first_try_success']}",
            f"Retries needed: {self.run_stats['retries']}",
            f"Complete failures: {self.run_stats['failures']}",
            f"Success rate: {success_rate:.2f}%",
            f"First try success rate: {first_try_rate:.2f}%",
            f"Average attempts per success: {avg_attempts:.2f}",
            f"Processing time: {duration:.2f} seconds",
            "=================================="
        ]
        
        return "\n".join(report)
    
    def _initialize_ocr(self):
        """Initialize the EasyOCR reader based on GPU settings."""
        try:
            if self.use_gpu:
                import torch
                try:
                    if self.gpu_device is not None:
                        torch.cuda.set_device(self.gpu_device)
                    gpu_name = torch.cuda.get_device_name(self.gpu_device or 0)
                    print(f"Captcha Solver: Using GPU device {self.gpu_device or 0}: {gpu_name}")
                    self.reader = easyocr.Reader(['en'], gpu=True)
                except Exception as e:
                    print(f"Captcha Solver: GPU error: {str(e)}. Falling back to CPU.")
                    self.reader = easyocr.Reader(['en'], gpu=False)
            else:
                print("Captcha Solver: Using CPU only (no GPU acceleration)")
                self.reader = easyocr.Reader(['en'], gpu=False)
        except Exception as e:
            print(f"Captcha Solver: Error initializing EasyOCR: {str(e)}")
            print("Captcha Solver: Make sure you have the required libraries installed:")
            print("pip install easyocr torch opencv-python pillow")
            raise
    
    def preprocess_captcha(self, image):
        """
        Apply multiple preprocessing techniques to the image to improve OCR accuracy.
        
        Args:
            image: Either a PIL Image or numpy array representing the captcha image.
            
        Returns:
            list: List of tuples (method_name, processed_image)
        """
        # Convert PIL image to numpy array for OpenCV
        if isinstance(image, Image.Image):
            img_np = np.array(image)
            # Convert RGB to BGR (OpenCV format)
            if len(img_np.shape) > 2 and img_np.shape[2] == 3:
                img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        else:
            img_np = image
            
        processed_images = []
        processed_images.append(("Original", img_np))
        
        # Method 1: Basic threshold
        if len(img_np.shape) > 2:
            gray = cv2.cvtColor(img_np, cv2.COLOR_BGR2GRAY)
        else:
            gray = img_np
            
        _, thresh1 = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
        processed_images.append(("Basic Threshold", thresh1))
        
        # Method 2: Adaptive threshold
        adaptive_thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2)
        processed_images.append(("Adaptive Threshold", adaptive_thresh))
        
        # Method 3: Otsu's thresholding
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        processed_images.append(("Otsu Threshold", otsu))
        
        # Method 4: Noise removal
        denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
        processed_images.append(("Denoised", denoised))
        
        # Method 5: Noise removal + threshold
        _, denoised_thresh = cv2.threshold(denoised, 127, 255, cv2.THRESH_BINARY)
        processed_images.append(("Denoised+Threshold", denoised_thresh))
        
        # Method 6: Dilated
        kernel = np.ones((2,2), np.uint8)
        dilated = cv2.dilate(gray, kernel, iterations=1)
        processed_images.append(("Dilated", dilated))
        
        # Method 7: Eroded
        eroded = cv2.erode(gray, kernel, iterations=1)
        processed_images.append(("Eroded", eroded))
        
        # Method 8: Edge enhancement
        edges = cv2.Canny(gray, 100, 200)
        processed_images.append(("Edges", edges))
        
        # Method 9: Morphological operations
        kernel = np.ones((1,1), np.uint8)
        opening = cv2.morphologyEx(gray, cv2.MORPH_OPEN, kernel)
        processed_images.append(("Opening", opening))
        
        # Method 10: Enhanced contrast
        if isinstance(image, Image.Image):
            pil_img = image
        else:
            if len(img_np.shape) > 2 and img_np.shape[2] == 3:
                pil_img = Image.fromarray(cv2.cvtColor(img_np, cv2.COLOR_BGR2RGB))
            else:
                pil_img = Image.fromarray(img_np)
                
        enhanced = ImageEnhance.Contrast(pil_img).enhance(2.0)
        enhanced_np = np.array(enhanced)
        if len(enhanced_np.shape) > 2 and enhanced_np.shape[2] == 3:
            enhanced_np = cv2.cvtColor(enhanced_np, cv2.COLOR_RGB2BGR)
        processed_images.append(("Enhanced Contrast", enhanced_np))
        
        # Method 11: Sharpened
        sharpened = pil_img.filter(ImageFilter.SHARPEN)
        sharpened_np = np.array(sharpened)
        if len(sharpened_np.shape) > 2 and sharpened_np.shape[2] == 3:
            sharpened_np = cv2.cvtColor(sharpened_np, cv2.COLOR_RGB2BGR)
        processed_images.append(("Sharpened", sharpened_np))
        
        # Method 12: Color filtering (for captchas with specific color text)
        if len(img_np.shape) > 2 and img_np.shape[2] == 3:
            # Extract blue channel
            blue_channel = img_np[:, :, 0]
            _, blue_thresh = cv2.threshold(blue_channel, 127, 255, cv2.THRESH_BINARY)
            processed_images.append(("Blue Channel", blue_thresh))
            
            # Create an HSV version and filter for common captcha colors
            hsv = cv2.cvtColor(img_np, cv2.COLOR_BGR2HSV)
            
            # Purple-blue range
            lower_purple = np.array([100, 50, 50])
            upper_purple = np.array([170, 255, 255])
            purple_mask = cv2.inRange(hsv, lower_purple, upper_purple)
            processed_images.append(("Purple Filter", purple_mask))
            
            # Green range
            lower_green = np.array([40, 50, 50])
            upper_green = np.array([90, 255, 255])
            green_mask = cv2.inRange(hsv, lower_green, upper_green)
            processed_images.append(("Green Filter", green_mask))
        
        return processed_images
    
    def save_captcha_image(self, img_np, fid, attempt, captcha_code):
        """
        Save a captcha image for debugging purposes.
        
        Args:
            img_np: Numpy array representing the image.
            fid: Player ID.
            attempt: Attempt number.
            captcha_code: Recognized captcha code.
            
        Returns:
            str: Path to the saved image, or None if saving failed.
        """
        if not self.save_images:
            return None
            
        try:
            timestamp = int(time.time())
            image_filename = f"fid{fid}_try{attempt}_OCR_{captcha_code}_{timestamp}.png"
            full_path = os.path.join(self.captcha_dir, image_filename)
            
            if cv2.imwrite(full_path, img_np):
                return full_path
            return None
        except Exception as e:
            print(f"Captcha Solver: Exception during saving captcha image: {str(e)}")
            return None
    
    async def solve_captcha(self, captcha_base64, fid="unknown", attempt=0):
        ocr_success = False
        best_result = None
        img_np = None
        try:
            self.stats["total_attempts"] += 1
            self.run_stats["total_attempts"] += 1

            if captcha_base64.startswith("data:image"):
                img_base64 = captcha_base64.split(",", 1)[1]
            else:
                img_base64 = captcha_base64

            img_bytes = base64.b64decode(img_base64)
            nparr = np.frombuffer(img_bytes, np.uint8)
            img_np = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            processed_images = self.preprocess_captcha(img_np)

            candidates = []
            loop = asyncio.get_event_loop()
            for method_name, processed_img in processed_images:
                if len(processed_img.shape) == 3 and processed_img.shape[2] == 3:
                    encode_success, encoded_bytes = cv2.imencode('.png', processed_img)
                elif len(processed_img.shape) == 2:
                    encode_success, encoded_bytes = cv2.imencode('.png', processed_img)
                else:
                    print(f"Captcha Solver: Warning - Unexpected image shape {processed_img.shape} for method {method_name}. Skipping.")
                    continue

                if not encode_success:
                     print(f"Captcha Solver: Warning - Failed to encode image for method {method_name}. Skipping.")
                     continue

                processed_bytes = encoded_bytes.tobytes()

                results = await loop.run_in_executor(None, lambda: self.reader.readtext(processed_bytes, detail=1))

                for result in results:
                    if len(result) >= 3:
                        text = result[1].strip().replace(' ', '')
                        confidence = result[2]
                        if text and confidence > self.min_confidence:
                            candidates.append((text, confidence, method_name))

            candidates.sort(key=lambda x: x[1], reverse=True)
            best_confidence = 0
            best_method = None

            for text, confidence, method_name in candidates:
                if text.isalnum() and len(text) == 4:
                    best_result = text
                    best_confidence = confidence
                    best_method = method_name
                    ocr_success = True
                    break

            should_save = False
            save_status_tag = "UNKNOWN"

            if ocr_success:
                if self.save_images_mode == 2 or self.save_images_mode == 3:
                    should_save = True
                    save_status_tag = best_result
            else:
                if self.save_images_mode == 1 or self.save_images_mode == 3:
                    should_save = True
                    save_status_tag = "FAILED"

            if should_save and img_np is not None:
                self.save_captcha_image(img_np, fid, attempt, save_status_tag)

            if ocr_success:
                self.stats["successful_decodes"] += 1
                self.run_stats["successful_decodes"] += 1

                if attempt == 0:
                    self.stats["first_try_success"] += 1
                    self.run_stats["first_try_success"] += 1
                else:
                    self.stats["retries"] += 1
                    self.run_stats["retries"] += 1

                return best_result, True, best_method, best_confidence
            else:
                self.stats["failures"] += 1
                self.run_stats["failures"] += 1
                return None, False, None, 0

        except Exception as e:
            print(f"Captcha Solver: Error solving captcha: {str(e)}")
            traceback.print_exc()
            self.stats["failures"] += 1
            self.run_stats["failures"] += 1

            # Save image on exception if configured and possible
            if (self.save_images_mode == 1 or self.save_images_mode == 3) and img_np is not None:
                try:
                    self.save_captcha_image(img_np, fid, attempt, "EXCEPTION")
                except Exception as save_err:
                     print(f"Captcha Solver: Error saving image during exception handling: {save_err}")

            return None, False, None, 0
    
    def get_stats(self):
        """Get current OCR statistics."""
        return self.stats