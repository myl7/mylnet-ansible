// Shortlink is a minimal personal URL shortener for my6.co.
//
// It serves 302 redirects from a flat mapping file (default /data/links.yaml).
// The file is re-read when its mtime changes, so updating a link is just an
// edit + git push + pull on the server; a parse error keeps the previous
// snapshot and is logged.
//
// The service intentionally logs no request information: these links are used
// in double-blind rebuttals, where the conferences require the host to not
// track visitors.
package main

import (
	"bufio"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

const defaultLinksPath = "/data/links.yaml"

// codeRe covers the single-character alphabet and keeps room for longer codes.
var codeRe = regexp.MustCompile(`^[A-Za-z0-9\-_.~]{1,16}$`)

type store struct {
	mu       sync.RWMutex
	links    map[string]string
	modTime  time.Time
	loadedOK bool
	lastErr  string
}

// load re-reads the mapping file when it changed. On any error the previous
// snapshot stays in place; only a changed error message is logged, so a
// permanently broken file does not spam the log every second.
func (s *store) load(path string) {
	f, err := os.Open(path)
	if err != nil {
		s.logErr(fmt.Sprintf("links: open %s: %v (keeping previous snapshot)", path, err))
		return
	}
	defer f.Close()

	fi, err := f.Stat()
	if err != nil {
		s.logErr(fmt.Sprintf("links: stat %s: %v (keeping previous snapshot)", path, err))
		return
	}
	if s.loadedOK && !fi.ModTime().After(s.modTime) {
		return
	}

	links, err := parse(f)
	if err != nil {
		s.logErr(fmt.Sprintf("links: %v (keeping previous snapshot)", err))
		return
	}
	s.mu.Lock()
	s.links = links
	s.modTime = fi.ModTime()
	s.loadedOK = true
	s.mu.Unlock()
	s.lastErr = ""
	log.Printf("links: loaded %d links from %s", len(links), path)
}

func (s *store) logErr(msg string) {
	if msg == s.lastErr {
		return
	}
	s.lastErr = msg
	log.Print(msg)
}

// parse reads the flat mapping format: one "code: target" pair per line,
// "#" comments and blank lines allowed. The format is a strict subset of
// YAML, rejected loudly on any deviation so the user sees the line number.
func parse(f *os.File) (map[string]string, error) {
	links := make(map[string]string)
	sc := bufio.NewScanner(f)
	for line := 1; sc.Scan(); line++ {
		text := strings.TrimSpace(sc.Text())
		if text == "" || strings.HasPrefix(text, "#") {
			continue
		}
		code, target, ok := strings.Cut(text, ":")
		if !ok {
			return nil, fmt.Errorf("line %d: missing ':'", line)
		}
		code = strings.TrimSpace(code)
		target = strings.TrimSpace(target)
		if !codeRe.MatchString(code) {
			return nil, fmt.Errorf("line %d: invalid code %q", line, code)
		}
		if err := validateTarget(target); err != nil {
			return nil, fmt.Errorf("line %d: %w", line, err)
		}
		links[code] = target
	}
	return links, sc.Err()
}

// validateTarget keeps the service from becoming an open redirect or a
// Location header injection vector.
func validateTarget(t string) error {
	if strings.ContainsAny(t, "\r\n") {
		return fmt.Errorf("target must not contain CR/LF")
	}
	u, err := url.Parse(t)
	if err != nil {
		return fmt.Errorf("invalid target URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return fmt.Errorf("target scheme %q not allowed", u.Scheme)
	}
	if u.Host == "" {
		return fmt.Errorf("target has no host")
	}
	if u.User != nil {
		return fmt.Errorf("target must not contain userinfo")
	}
	return nil
}

func (s *store) handler(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/healthz":
		w.Write([]byte("ok"))
		return
	case "/":
		http.NotFound(w, r)
		return
	}

	code := strings.TrimPrefix(r.URL.Path, "/")
	if strings.Contains(code, "/") || !codeRe.MatchString(code) {
		http.NotFound(w, r)
		return
	}

	s.mu.RLock()
	target, ok := s.links[code]
	s.mu.RUnlock()
	if !ok {
		http.NotFound(w, r)
		return
	}

	// Refuse redirect loops back to this service.
	if u, err := url.Parse(target); err == nil && strings.EqualFold(u.Host, r.Host) {
		http.NotFound(w, r)
		return
	}

	// 302 + no-store so a code can be repointed mid-rebuttal without stale
	// browser or proxy caches.
	w.Header().Set("Location", target)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusFound)
}

func main() {
	path := defaultLinksPath
	if len(os.Args) > 1 {
		path = os.Args[1]
	}

	s := &store{}
	s.load(path)
	go func() {
		for range time.Tick(time.Second) {
			s.load(path)
		}
	}()

	http.HandleFunc("/", s.handler)
	log.Printf("shortlink: listening on :8080, links from %s", path)
	log.Fatal(http.ListenAndServe(":8080", nil))
}
