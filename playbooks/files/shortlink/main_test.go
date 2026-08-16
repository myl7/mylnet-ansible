package main

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeLinks(t *testing.T, dir, content string) string {
	t.Helper()
	path := filepath.Join(dir, "links.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestParse(t *testing.T) {
	dir := t.TempDir()
	path := writeLinks(t, dir, `
# rebuttal figures
a: https://anonymous.4open.science/r/paper1-ABCD/
Z: https://example.com/x:8080/path
`)

	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()

	links, err := parse(f)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if links["a"] != "https://anonymous.4open.science/r/paper1-ABCD/" {
		t.Errorf("a = %q", links["a"])
	}
	if links["Z"] != "https://example.com/x:8080/path" {
		t.Errorf("Z = %q", links["Z"])
	}
}

func TestParseRejects(t *testing.T) {
	dir := t.TempDir()
	cases := map[string]string{
		"missing colon":  "a https://example.com\n",
		"invalid code":   "a b: https://example.com\n",
		"bad scheme":     "a: javascript:alert(1)\n",
		"no host":        "a: https://\n",
		"userinfo":       "a: https://user:pass@example.com/\n",
		"CRLF injection": "a: https://example.com/\r\nX-Evil: 1\n",
		"non-ASCII code": "好: https://example.com\n",
		"empty target":   "a:\n",
	}
	for name, content := range cases {
		path := writeLinks(t, dir, content)
		f, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := parse(f); err == nil {
			t.Errorf("%s: expected error, got none", name)
		}
		f.Close()
	}
}

func TestHandler(t *testing.T) {
	s := &store{
		links:    map[string]string{"a": "https://anonymous.4open.science/r/paper1-ABCD/"},
		loadedOK: true,
	}

	t.Run("redirect", func(t *testing.T) {
		rec := httptest.NewRecorder()
		s.handler(rec, httptest.NewRequest(http.MethodGet, "https://my6.co/a", nil))
		if rec.Code != http.StatusFound {
			t.Fatalf("code = %d, want 302", rec.Code)
		}
		if got := rec.Header().Get("Location"); got != "https://anonymous.4open.science/r/paper1-ABCD/" {
			t.Errorf("Location = %q", got)
		}
		if got := rec.Header().Get("Cache-Control"); got != "no-store" {
			t.Errorf("Cache-Control = %q", got)
		}
	})

	t.Run("healthz", func(t *testing.T) {
		rec := httptest.NewRecorder()
		s.handler(rec, httptest.NewRequest(http.MethodGet, "https://my6.co/healthz", nil))
		if rec.Code != http.StatusOK {
			t.Errorf("code = %d, want 200", rec.Code)
		}
	})

	t.Run("unknown code", func(t *testing.T) {
		rec := httptest.NewRecorder()
		s.handler(rec, httptest.NewRequest(http.MethodGet, "https://my6.co/zz", nil))
		if rec.Code != http.StatusNotFound {
			t.Errorf("code = %d, want 404", rec.Code)
		}
	})

	t.Run("invalid path", func(t *testing.T) {
		rec := httptest.NewRecorder()
		s.handler(rec, httptest.NewRequest(http.MethodGet, "https://my6.co/a/b", nil))
		if rec.Code != http.StatusNotFound {
			t.Errorf("code = %d, want 404", rec.Code)
		}
	})

	t.Run("self loop", func(t *testing.T) {
		loop := &store{
			links:    map[string]string{"x": "https://my6.co/a"},
			loadedOK: true,
		}
		rec := httptest.NewRecorder()
		loop.handler(rec, httptest.NewRequest(http.MethodGet, "https://my6.co/x", nil))
		if rec.Code != http.StatusNotFound {
			t.Errorf("code = %d, want 404", rec.Code)
		}
	})
}

func TestReloadKeepsSnapshotOnError(t *testing.T) {
	dir := t.TempDir()
	path := writeLinks(t, dir, "a: https://example.com/one\n")

	s := &store{}
	s.load(path)
	if s.links["a"] != "https://example.com/one" {
		t.Fatalf("initial load failed: %v", s.links)
	}

	// Repoint the code.
	writeLinks(t, dir, "a: https://example.com/two\n")
	// Force a newer mtime; writeLinks may reuse the second.
	now := time.Now().Add(time.Second)
	os.Chtimes(path, now, now)
	s.load(path)
	if s.links["a"] != "https://example.com/two" {
		t.Fatalf("reload did not pick up new target: %v", s.links)
	}

	// Break the file: the previous snapshot must survive.
	writeLinks(t, dir, "a: https://example.com/two\nbad line\n")
	os.Chtimes(path, time.Now().Add(2*time.Second), time.Now().Add(2*time.Second))
	s.load(path)
	if s.links["a"] != "https://example.com/two" {
		t.Fatalf("broken file lost the snapshot: %v", s.links)
	}
	if !strings.Contains(s.lastErr, "line 2") {
		t.Errorf("lastErr = %q, want line number", s.lastErr)
	}
}
