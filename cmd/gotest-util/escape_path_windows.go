//go:build windows

package main

import "strings"

var windowsPathReplacer = strings.NewReplacer(
	`*`, `%`,
	`.`, `%`,
	`"`, `%`,
	`/`, `%`,
	`\`, `%`,
	`[`, `%`,
	`]`, `%`,
	`:`, `%`,
	`;`, `%`,
	`|`, `%`,
	`,`, `%`,
)

func escapePath(s string) string {
	if shouldHashPath(s) {
		return hashEscapePath(s)
	}
	return windowsPathReplacer.Replace(s)
}
