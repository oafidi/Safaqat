/** ZenGrid tokens carried onto Safaqat's existing palette. */
module.exports = {
  content: ["./*.html", "./*.js"],
  theme: {
    // The grid is flat by definition: no elevation scale exists.
    boxShadow: { none: "none" },
    borderRadius: {
      none: "0",
      sm: "2px",
      DEFAULT: "4px",
      md: "4px",
      lg: "6px",
      full: "9999px",
    },
    extend: {
      colors: {
        ink: { DEFAULT: "#172033", muted: "#536077", faint: "#5F6A7C" },
        canvas: { DEFAULT: "#F6F5F1", sunken: "#EDECE7", raised: "#FFFFFF" },
        line: { DEFAULT: "#DCE0E7", strong: "#C2C9D5" },
        accent: { DEFAULT: "#B9342D", hover: "#922720", active: "#7A1F1A" },
        onink: { DEFAULT: "#FFFFFF", muted: "#A9B4C9", faint: "#7F8CA5" },
        onaccent: { muted: "#F3D6D3" },
        state: { success: "#0F6B44", warning: "#8A5A05", error: "#A42017" },
      },
      fontFamily: {
        display: ['"Raleway"', "ui-sans-serif", "system-ui", "sans-serif"],
        body: ['"DM Sans"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"Fira Code"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        tiny: ["11px", { lineHeight: "1.4", letterSpacing: "0.08em" }],
        small: ["13px", { lineHeight: "1.6" }],
        body: ["15px", { lineHeight: "1.7" }],
        h4: ["18px", { lineHeight: "1.35" }],
        h3: ["24px", { lineHeight: "1.25" }],
        h2: ["clamp(26px, 3.4vw, 32px)", { lineHeight: "1.2", letterSpacing: "-0.012em" }],
        h1: ["clamp(30px, 5vw, 40px)", { lineHeight: "1.15", letterSpacing: "-0.015em" }],
      },
      // Base unit 12px: every step is a multiple of 6.
      spacing: { 1.5: "6px", 3: "12px", 6: "24px", 9: "36px", 12: "48px", 18: "72px", 24: "96px", 30: "120px" },
      maxWidth: { measure: "68ch" },
    },
  },
  plugins: [],
};
