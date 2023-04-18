//go:build !windows

package main

import (
	"path/filepath"
	"strings"
)

func escapePath(s string) string {
	if shouldHashPath(s) {
		return hashEscapePath(s)
	}
	return strings.Replace(filepath.ToSlash(s), "/", "%", -1)
}
