package main

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
)

func shouldHashPath(s string) bool {
	// The max file name is 255 on Darwin and 259 on Windows so use 254 to be safe.
	return len(s) >= 254-len(".test.exe")
}

func hashEscapePath(s string) string {
	h := sha256.Sum256([]byte(filepath.Clean(s)))
	sum := hex.EncodeToString(h[:8])
	return sum + "." + filepath.Base(s) + ".test.exe" // Add the ".exe" for Windows
}
