export interface CanvasPoint {
  x: number;
  y: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/**
 * Compute the rendered content rectangle inside a canvas element, accounting
 * for CSS `object-fit` letterboxing (`contain` / `scale-down` / `cover`).
 */
function getRenderedContentRect(
  canvas: HTMLCanvasElement,
  rect: DOMRect,
): { left: number; top: number; width: number; height: number } {
  const objectFit =
    typeof window !== "undefined" && typeof window.getComputedStyle === "function"
      ? window.getComputedStyle(canvas).objectFit
      : "";

  if (
    canvas.width > 0 &&
    canvas.height > 0 &&
    (objectFit === "contain" || objectFit === "scale-down" || objectFit === "cover")
  ) {
    const scaleX = rect.width / canvas.width;
    const scaleY = rect.height / canvas.height;
    let scale = objectFit === "cover" ? Math.max(scaleX, scaleY) : Math.min(scaleX, scaleY);
    if (objectFit === "scale-down") {
      scale = Math.min(scale, 1);
    }
    const width = canvas.width * scale;
    const height = canvas.height * scale;
    return {
      left: rect.left + (rect.width - width) / 2,
      top: rect.top + (rect.height - height) / 2,
      width,
      height,
    };
  }

  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  };
}

/** Map a screen pointer position to canvas pixel coordinates. */
export function getCanvasCoords(
  canvas: HTMLCanvasElement | null,
  e: { clientX: number; clientY: number },
): CanvasPoint {
  if (!canvas) return { x: 0, y: 0 };
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0 || canvas.width === 0 || canvas.height === 0) {
    return { x: 0, y: 0 };
  }
  const content = getRenderedContentRect(canvas, rect);
  if (content.width === 0 || content.height === 0) return { x: 0, y: 0 };
  const scaleX = canvas.width / content.width;
  const scaleY = canvas.height / content.height;
  return {
    x: clamp(Math.round((e.clientX - content.left) * scaleX), 0, canvas.width),
    y: clamp(Math.round((e.clientY - content.top) * scaleY), 0, canvas.height),
  };
}

/** Paint a JPEG base64 frame onto a canvas (WebSocket stream). */
export function paintBase64JpegToCanvas(
  canvas: HTMLCanvasElement | null,
  base64Data: string,
): void {
  if (!canvas) return;
  const img = new Image();
  img.onload = () => {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (canvas.width !== img.width || canvas.height !== img.height) {
      canvas.width = img.width;
      canvas.height = img.height;
    }
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) {
      ctx.imageSmoothingQuality = "high";
    }
    ctx.drawImage(img, 0, 0);
  };
  img.src = `data:image/jpeg;base64,${base64Data}`;
}

/** Paint a screenshot blob onto a canvas (HTTP polling). */
export async function paintBlobToCanvas(
  canvas: HTMLCanvasElement | null,
  blob: Blob,
): Promise<boolean> {
  if (!canvas) return false;
  const bitmap = await createImageBitmap(blob);
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  bitmap.close();
  return true;
}

export function clearCanvas(canvas: HTMLCanvasElement | null): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
}
