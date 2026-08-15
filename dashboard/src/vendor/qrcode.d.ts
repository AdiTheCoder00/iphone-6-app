/* Hand-written types for the vendored qrcode-generator. Only the four members
 * the pairing dialog actually calls are declared — the library surface is much
 * larger, but anything not declared here is not used, and a fuller definition
 * would be untested guesswork. */

export interface QRCode {
  /** `mode` is the encoding hint; 'Byte' is correct for arbitrary URLs. */
  addData(data: string, mode?: 'Numeric' | 'Alphanumeric' | 'Byte' | 'Kanji'): void;
  /** Runs the encoder. Must be called before getModuleCount/isDark. */
  make(): void;
  /** Width of the symbol in modules (it is square). */
  getModuleCount(): number;
  /** True when the module at (row, col) should be painted dark. */
  isDark(row: number, col: number): boolean;
}

/**
 * @param typeNumber QR version 1–40, or 0 to pick the smallest that fits.
 * @param errorCorrectionLevel L/M/Q/H — increasing redundancy.
 */
declare function qrcode(
  typeNumber: number,
  errorCorrectionLevel: 'L' | 'M' | 'Q' | 'H',
): QRCode;

export default qrcode;
