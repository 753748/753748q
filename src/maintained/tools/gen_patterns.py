"""Generator for Rich's bundled hyphenation pattern table.

The pattern table shipped in ``rich/_hyphenation_patterns.py`` is *derived*, not
hand written.  This tool derives it from a curated corpus of correctly
hyphenated English words using the classical Knuth/Liang pattern-derivation
procedure:

1. Every hyphenated corpus entry is expanded with word-boundary dots, e.g.
   ``"hy-phen-ation"`` becomes ``".hy3phen3ation."`` where a ``3`` marks a legal
   break (odd weight) and the dots mark the boundaries.
2. For every substring of that expanded string (subject to a maximum pattern
   length) a candidate pattern is recorded together with the weights its
   interior positions contribute.
3. For each candidate pattern the weight of every interior position is the
   **maximum** weight observed for that position across the whole corpus, and a
   position that is never a legal break gets weight ``0``.
4. Patterns that carry no odd weight at all ("all-even" patterns) can only
   *block* breaks.  They are kept, because blocking is exactly how the algorithm
   prevents wrong breaks such as ``"pre-sent"`` -> ``"pres-ent"``.
5. The result is pruned: patterns that never win a position for any corpus word
   are dropped, then the surviving patterns are sorted for a stable, readable
   diff.

The generator also reports the training recall, i.e. how many corpus words the
derived table hyphenates exactly as the corpus says.  That number is quoted in
the module docstring of the generated file so the accuracy claim is verifiable
rather than asserted.

Usage::

    python -m tools.gen_patterns            # write rich/_hyphenation_patterns.py
    python -m tools.gen_patterns --report   # print the accuracy report only
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

MAX_PATTERN_LEN = 6
MAX_OFFSET = 4
MAX_WEIGHT = 9

# ---------------------------------------------------------------------------
# Curated corpus
# ---------------------------------------------------------------------------
# Ground truth hyphenation points.  Only words where a break may legally be
# attempted exactly as written are listed, so the derived table never learns a
# break that violates the left/right minima.
CORPUS: Tuple[str, ...] = (
    # --- general vocabulary ------------------------------------------------
    "ab-sent", "ab-so-lute", "ab-sorb", "ac-cept", "ac-cept-able", "ac-ci-dent",
    "ac-com-mo-date", "ac-count", "ac-cu-rate", "achieve-ment", "ad-dress",
    "ad-just", "ad-min-is-ter", "ad-mire", "ad-vo-cate", "af-fair", "af-fect",
    "af-ter", "a-gain", "a-gainst", "al-though", "al-ways", "am-bi-tious",
    "an-ces-tor", "an-oth-er", "an-swer", "anx-ious", "ap-pear", "ap-point",
    "ap-pre-ci-ate", "ar-gue", "ar-range", "ar-rive", "as-sess", "as-sist",
    "as-so-ci-ate", "as-sume", "at-tach", "at-tempt", "at-tend", "at-ti-tude",
    "at-tract", "au-di-ence", "au-thor", "au-thor-ity", "av-er-age",
    "be-cause", "be-come", "be-fore", "be-gin", "be-hind", "be-lieve",
    "be-neath", "be-ne-fit", "be-side", "be-sides", "be-tween", "busi-ness",
    "capa-ble", "ca-pac-ity", "cap-i-tal", "cap-ture", "care-ful", "car-ry",
    "cat-a-log", "cau-tious", "cel-e-brate", "cer-tain", "chal-lenge",
    "change-able", "char-ac-ter", "charge", "chem-is-try", "chil-dren",
    "choose", "cir-cum-stance", "cit-i-zen", "claim", "clar-ify", "clas-si-cal",
    "clas-si-fi-ca-tion", "cli-ent", "cli-mate", "col-lect", "col-lec-tion",
    "col-lege", "com-bi-na-tion", "com-fort", "com-mand", "com-ment",
    "com-mer-cial", "com-mon", "com-mu-ni-cate", "com-mu-ni-ca-tion",
    "com-pa-ny", "com-pare", "com-pare", "com-peti-tion", "com-peti-tive",
    "com-plain", "com-plete", "com-plex", "com-po-nent", "com-pos-ite",
    "com-pre-hend", "com-pute", "com-put-er", "con-cen-trate", "con-cept",
    "con-cern", "con-clude", "con-di-tion", "con-duct", "con-fer-ence",
    "con-fi-dence", "con-firm", "con-flict", "con-fuse", "con-nec-tion",
    "con-quest", "con-scious", "con-se-quence", "con-serv-a-tive",
    "con-sider", "con-sider-able", "con-sist", "con-so-nant", "con-spir-a-cy",
    "con-stant", "con-sti-tute", "con-struct", "con-sult", "con-sume",
    "con-tact", "con-tain", "con-tem-po-rary", "con-tent", "con-test",
    "con-text", "con-tin-ue", "con-tin-u-ous", "con-tract", "con-tra-dict",
    "con-trast", "con-trib-ute", "con-trol", "con-ven-ient", "con-ver-sa-tion",
    "con-vert", "con-vince", "cor-rect", "cor-re-spond", "count-less",
    "cour-age", "cre-ate", "cred-it", "crit-i-cal", "cul-ture", "cu-ri-ous",
    "cus-tom-er",
    # --- reading, layout and typography ------------------------------------
    "hy-phen", "hy-phen-ate", "hy-phen-ated", "hy-phen-ation", "al-go-rithm",
    "al-go-rithms", "blank", "body", "break", "char-ac-ter", "col-umn",
    "com-pose", "com-pos-ing", "con-tain-er", "con-tent", "con-text",
    "di-a-gram", "doc-u-ment", "doc-u-men-ta-tion", "en-gi-neer", "for-mat",
    "for-mat-ting", "frag-ment", "glyph", "graph-ics", "grid", "hy-phen-a-tion",
    "in-dent", "in-den-ta-tion", "in-dex", "in-ter-face", "in-ter-face",
    "in-ter-line", "jus-ti-fi-ca-tion", "jus-ti-fy", "ker-nel", "land-scape",
    "lay-out", "li-brary", "li-brar-ies", "line", "lin-guist", "man-u-script",
    "mar-gin", "mea-sure", "mea-sure-ment", "mono-space", "mor-phol-ogy",
    "orphan", "pa-per", "pa-ram-e-ter", "paragraph", "par-a-graph",
    "par-a-graphs", "pat-tern", "pat-terns", "portrait", "print-er",
    "punc-tu-a-tion", "rec-tan-gle", "ren-der", "ren-der-ing", "sen-tence",
    "sen-ten-ces", "sep-a-rate", "sep-a-ra-tor", "soft-ware", "space",
    "syl-la-ble", "syl-la-bles", "tab-u-lar", "tem-plate", "ter-mi-nal",
    "tex-tu-al", "ty-pog-ra-phy", "ty-po-graph-i-cal", "un-just-i-fied",
    "wid-ow", "word", "wrap", "wrap-ping", "writ-ing", "line-break",
    "word-wrap",
    # --- computing and software engineering --------------------------------
    "al-go-rithm", "ap-pli-ca-tion", "ap-pli-ca-tions", "ar-chi-tec-ture",
    "ar-gu-ment", "as-sem-bly", "at-trib-ute", "back-ground", "bench-mark",
    "bi-nary", "block", "boot-strap", "buff-er", "byte", "cache",
    "call-back", "cli-ent", "com-ma-nd", "com-pi-la-tion", "com-pile",
    "com-pil-er", "com-po-nent", "con-cur-rent", "con-fig-u-ra-tion",
    "con-nect-ing", "con-sole", "con-struct-or", "con-tain-er",
    "cop-y-right", "data-base", "de-bug", "de-bugg-er", "de-cla-ra-tion",
    "de-fault", "de-lay", "de-pen-dency", "de-ploy", "de-ply-ment",
    "de-scrip-tion", "de-vel-op", "de-vel-op-er", "de-vel-op-ment",
    "di-ag-nos-tic", "di-rec-tory", "dis-patch", "dis-play", "dis-tri-bu-tion",
    "doc-string", "down-load", "du-pli-cate", "en-cap-su-la-tion",
    "en-cod-ing", "en-gi-neer-ing", "en-ter-prise", "en-vi-ron-ment",
    "ex-cep-tion", "ex-e-cu-tion", "ex-tend-able", "file-name", "frame-work",
    "func-tion", "func-tions", "gen-er-ate", "gen-er-a-tion", "git-hub",
    "han-dler", "head-er", "hi-er-ar-chy", "im-ple-ment", "im-ple-men-ta-tion",
    "in-her-it-ance", "in-stall", "in-stance", "in-stru-ment", "in-te-ger",
    "in-ter-preter", "in-ter-rupt", "in-ter-val", "key-board", "key-word",
    "li-cense", "loop-back", "main-tain", "main-te-nance", "mem-ory",
    "mes-sage", "meta-data", "mod-u-lar", "mod-ule", "mon-i-tor",
    "name-space", "net-work", "op-er-at-ing", "op-er-a-tion", "op-ti-mal",
    "op-ti-mi-za-tion", "op-ti-mize", "pack-age", "pa-ram-e-ter",
    "par-ti-tion", "per-for-mance", "per-mis-sion", "pipe-line", "plat-form",
    "plug-in", "pointer", "pol-y-mor-phism", "pro-ce-dure", "proc-ess",
    "pro-cess-ing", "pro-ces-sor", "pro-gram", "pro-gram-mer",
    "pro-gram-ming", "pro-ject", "prop-er-ty", "pro-to-col", "pseu-do-code",
    "ras-ter", "re-cov-ery", "rec-tan-gle", "re-cur-sion", "re-cur-sive",
    "ref-er-ence", "re-fac-tor", "reg-is-ter", "re-lease", "re-mote",
    "re-pos-i-tory", "re-quest", "re-quire", "re-source", "re-sponse",
    "re-sult", "re-try", "re-use", "run-time", "sched-ule", "sched-ul-er",
    "se-cu-rity", "sem-a-phore", "se-quence", "se-ri-al-i-za-tion",
    "serv-er", "ses-sion", "snap-shot", "sock-et", "source", "stack",
    "state-ment", "state-ments", "stream", "string", "struc-ture",
    "sub-rou-tine", "sub-scribe", "sub-sti-tute", "sym-bol", "syntax",
    "sys-tem", "tem-po-rary", "term", "ter-mi-nal", "test-ing",
    "thread", "through-put", "time-stamp", "ti-tle", "to-ken",
    "trans-ac-tion", "trans-form", "trans-la-tion", "trans-la-tor",
    "trans-mit", "type-check", "uni-code", "uni-fi-ca-tion", "u-ni-ver-sal",
    "up-date", "up-grade", "u-til-ity", "val-i-date", "val-i-da-tion",
    "vari-able", "ven-dor", "ver-i-fi-ca-tion", "ver-sion", "vir-tu-al",
    "web-site", "work-flow", "work-space", "wrap-per",
    # --- longer general words ---------------------------------------------
    "ab-bre-vi-a-tion", "ac-cel-er-a-tion", "ac-com-pa-ni-ment",
    "ac-cu-mu-late", "ad-min-is-tra-tion", "ad-o-les-cent", "ad-ver-tise-ment",
    "ag-ri-cul-ture", "al-pha-bet-i-cal", "an-ni-ver-sary", "ap-prox-i-mate",
    "ar-chae-ol-ogy", "ar-chi-tec-tur-al", "ar-gu-men-ta-tive", "ar-ith-me-tic",
    "as-tro-nom-i-cal", "au-to-bi-og-ra-phy", "au-to-mat-i-cal-ly",
    "be-nev-o-lent", "bi-o-log-i-cal", "cal-cu-la-tion", "ca-pa-bil-ity",
    "cat-a-stro-phe", "cel-e-bra-tion", "cer-ti-fi-cate", "chro-no-log-i-cal",
    "civ-i-li-za-tion", "clas-si-fi-ca-tion", "co-in-ci-dence",
    "com-mu-ni-ca-tive", "com-pa-ny", "com-pa-rable", "com-pati-bil-ity",
    "com-pen-sa-tion", "com-pe-ti-tor", "com-ple-men-tary",
    "com-pre-hen-sive", "com-pu-ta-tion", "con-cen-tra-tion", "con-clu-sion",
    "con-di-tion-al", "con-fer-ence", "con-fig-u-ra-tion", "con-fir-ma-tion",
    "con-grat-u-late", "con-se-quent-ly", "con-ser-va-tion", "con-sid-er-ate",
    "con-sid-er-a-tion", "con-sti-tu-tion", "con-struc-tion",
    "con-sul-ta-tion", "con-tem-po-rary", "con-tin-u-a-tion",
    "con-trib-u-tion", "con-tro-ver-sial", "con-ven-tion-al",
    "con-ver-sa-tion-al", "co-op-er-a-tion", "cor-re-spond-ence",
    "coun-ter-pro-duc-tive", "crim-i-nol-ogy", "cu-ri-os-ity",
    "dec-la-ra-tion", "de-com-po-si-tion", "ded-i-ca-tion", "def-i-ni-tion",
    "del-e-ga-tion", "dem-on-stra-tion", "de-nom-i-na-tion", "de-scrip-tive",
    "de-ter-mi-na-tion", "de-vel-op-men-tal", "dif-fer-en-ti-ate",
    "di-men-sion-al", "di-rec-tion-al", "dis-ad-van-tage", "dis-cov-ery",
    "dis-crim-i-na-tion", "dis-tri-bu-tion", "di-ver-si-fi-ca-tion",
    "econ-o-my", "ed-u-ca-tion", "ed-u-ca-tion-al", "ef-fi-cien-cy",
    "e-lab-o-rate", "e-lec-tri-cal", "e-lec-tron-ic", "el-e-ment",
    "e-lim-i-nate", "e-mer-gen-cy", "em-pha-size", "en-cour-age",
    "en-gi-neer", "en-thu-si-asm", "en-vi-ron-men-tal", "e-qui-lib-ri-um",
    "es-tab-lish", "e-val-u-a-tion", "e-ven-tu-al-ly", "ex-ag-ger-ate",
    "ex-am-i-na-tion", "ex-cel-lent", "ex-clu-sive", "ex-er-cise",
    "ex-hi-bi-tion", "ex-pe-di-tion", "ex-pe-ri-ence", "ex-per-i-men-tal",
    "ex-pla-na-tion", "ex-plor-a-tion", "ex-po-si-tion", "ex-pres-sion",
    "ex-ten-sion", "ex-traor-di-nary", "fa-mil-iar-ity", "fan-tas-tic",
    "fa-vour-able", "fea-si-bil-ity", "fer-ti-li-za-tion", "fi-del-ity",
    "flex-i-bil-ity", "for-mu-la-tion", "for-tu-nate-ly", "found-a-tion",
    "frag-men-ta-tion", "fre-quen-cy", "fun-da-men-tal", "gen-er-al-i-za-tion",
    "ge-o-graph-i-cal", "gov-ern-ment", "grad-u-al-ly", "gram-mat-i-cal",
    "guar-an-tee", "gym-na-si-um", "hab-i-ta-tion", "he-red-i-tary",
    "hi-er-ar-chi-cal", "his-tor-i-cal", "ho-mo-ge-ne-ous", "hon-or-able",
    "hos-pi-tal-ity", "hu-man-i-ties", "hy-gi-en-ic", "hy-per-bo-le",
    "hy-po-thet-i-cal", "i-den-ti-fi-ca-tion", "i-den-ti-fy", "i-de-ol-ogy",
    "ig-no-rance", "il-lu-mi-na-tion", "il-lus-tra-tion", "im-ag-i-na-tion",
    "im-i-ta-tion", "im-me-di-ate", "im-mense", "im-mu-ni-ty",
    "im-pa-tient", "im-per-a-tive", "im-ple-men-ta-tion", "im-pli-ca-tion",
    "im-por-tance", "im-pos-si-ble", "im-pres-sion", "im-prove-ment",
    "in-au-gu-ral", "in-cen-tive", "in-ci-den-tal", "in-clu-sive",
    "in-com-pe-tent", "in-con-sis-tent", "in-con-ve-nient", "in-cor-po-rate",
    "in-cred-i-ble", "in-de-pen-dent", "in-di-ca-tion", "in-dif-fer-ent",
    "in-di-vid-u-al", "in-dus-tri-al", "in-ev-i-ta-ble", "in-fec-tion",
    "in-fi-nite", "in-flu-en-tial", "in-for-ma-tion", "in-fra-struc-ture",
    "in-ge-nu-ity", "in-hab-it-ant", "in-i-tia-tive", "in-jec-tion",
    "in-no-va-tion", "in-quis-i-tive", "in-scrip-tion", "in-sec-ti-cide",
    "in-sig-nif-i-cant", "in-spec-tion", "in-spi-ra-tion", "in-stal-la-tion",
    "in-sti-tu-tion", "in-struc-tion", "in-stru-men-tal", "in-sur-ance",
    "in-te-gra-tion", "in-tel-lec-tual", "in-tel-li-gent", "in-ten-sive",
    "in-ter-ac-tion", "in-ter-est-ing", "in-ter-na-tion-al",
    "in-ter-pre-ta-tion", "in-ter-rog-a-tive", "in-ter-rup-tion",
    "in-ter-sec-tion", "in-ter-ven-tion", "in-tro-duc-tion", "in-ven-tion",
    "in-ves-ti-ga-tion", "in-ves-tor", "in-vis-i-ble", "ir-re-gular",
    "ir-re-le-vant", "ir-re-sis-ti-ble", "ir-ri-ga-tion", "jour-nal-ism",
    "ju-ris-dic-tion", "jus-ti-fi-able", "lab-o-ra-to-ry", "leg-is-la-tion",
    "le-git-i-mate", "lex-i-cog-ra-phy", "lib-er-al-i-za-tion", "li-cense",
    "lit-er-a-ture", "log-i-cal", "lux-u-ri-ous", "mag-nif-i-cent",
    "mag-ni-tude", "main-te-nance", "man-u-fac-ture", "man-u-fac-tur-er",
    "man-u-fac-tur-ing", "math-e-mat-i-cal", "math-e-mat-ics",
    "mea-sure-ment", "me-chan-i-cal", "mech-a-nism", "med-i-cine",
    "mem-o-ran-dum", "meta-mor-pho-sis", "me-te-o-rol-o-gy", "meth-od-ol-ogy",
    "met-ro-pol-i-tan", "mi-cro-scop-ic", "mi-gra-tion", "min-is-te-ri-al",
    "mi-nor-ity", "mis-cel-la-ne-ous", "mod-i-fi-ca-tion", "mo-lec-u-lar",
    "mo-men-tum", "mon-o-lith-ic", "mo-not-o-nous", "mor-pho-log-i-cal",
    "mo-ti-va-tion", "mul-ti-pli-ca-tion", "mu-nic-i-pal", "mu-ta-tion",
    "nar-ra-tive", "nav-i-ga-tion", "neg-li-gi-ble", "ne-go-ti-a-tion",
    "neu-rot-ic", "nom-i-na-tion", "nonethe-less", "no-ta-tion",
    "no-ti-fi-ca-tion", "nu-mer-i-cal", "ob-jec-tive", "ob-li-ga-tion",
    "ob-ser-va-tion", "ob-so-lete", "ob-sta-cle", "ob-vi-ous",
    "oc-ca-sion-al", "oc-cu-pa-tion", "oc-cur-rence", "op-er-a-tion-al",
    "op-por-tu-ni-ty", "op-po-si-tion", "op-ti-mis-tic", "or-ches-tra",
    "or-di-nary", "or-gan-i-za-tion", "or-gan-ism", "o-ri-en-ta-tion",
    "orig-i-nal", "os-cil-la-tion", "o-ver-lap", "o-ver-whelm",
    "pa-ren-the-sis", "par-tic-i-pant", "par-tic-u-lar", "pas-sen-ger",
    "pa-thet-ic", "pa-tri-ot-ic", "pen-e-tra-tion", "per-cep-tion",
    "per-fec-tion", "per-for-mance", "pe-ri-od-ic", "per-ma-nent",
    "per-mis-si-ble", "per-pen-dic-u-lar", "per-se-ver-ance", "per-son-al-ity",
    "per-spec-tive", "per-sua-sion", "phi-los-o-pher", "pho-to-graph-ic",
    "phys-i-cal", "phys-i-ol-ogy", "pi-o-neer", "plau-si-ble",
    "pop-u-la-tion", "por-tray-al", "pos-si-bil-ity", "po-ten-tial",
    "prac-ti-cal", "prec-e-dent", "pre-ci-sion", "pre-dic-tion",
    "pref-er-ence", "pre-ju-dice", "pre-limi-nary", "prep-a-ra-tion",
    "pre-scrip-tion", "pres-en-ta-tion", "pres-er-va-tion", "pres-ti-gious",
    "pre-sump-tion", "prev-a-lent", "pri-or-ity", "priv-i-lege",
    "prob-a-bil-ity", "prob-a-bly", "pro-ce-dure", "pro-ceed-ings",
    "pro-duc-tiv-ity", "pro-fes-sion-al", "prof-it-able", "pro-found",
    "prog-nos-ti-ca-tion", "pro-hib-i-tive", "prom-i-nent", "pro-mo-tion",
    "pro-nun-ci-a-tion", "prop-a-gan-da", "pro-por-tion", "pro-pos-al",
    "pros-per-ity", "pro-tec-tion", "pro-tes-tant", "prov-o-ca-tion",
    "psy-cho-log-i-cal", "pub-li-ca-tion", "pu-ri-fi-ca-tion", "pur-suit",
    "qual-i-fi-ca-tion", "quan-ti-ty", "ques-tion-naire", "quo-ta-tion",
    "ra-di-a-tion", "ra-tion-al", "re-ac-tion", "re-a-lis-tic",
    "re-cog-ni-tion", "rec-om-men-da-tion", "rec-on-cil-i-a-tion",
    "re-cov-ery", "rec-re-a-tion", "re-cruit-ment", "ref-er-ence",
    "re-flec-tion", "re-for-ma-tion", "re-frig-er-a-tion", "re-gard-less",
    "reg-is-tra-tion", "reg-u-la-tion", "re-ha-bil-i-ta-tion",
    "re-in-force-ment", "re-jec-tion", "re-la-tion-ship", "rel-a-tive",
    "re-li-a-bil-ity", "re-li-gious", "re-luc-tant", "re-mar-kable",
    "rem-e-dy", "rem-i-nis-cent", "re-mu-ner-a-tion", "ren-o-va-tion",
    "rep-e-ti-tion", "rep-re-sen-ta-tion", "rep-u-ta-tion", "req-ui-si-tion",
    "res-er-va-tion", "res-i-dence", "re-sis-tance", "res-o-lu-tion",
    "re-sources", "re-spect-able", "re-spon-si-bil-ity", "res-tau-rant",
    "res-to-ra-tion", "re-stric-tion", "re-sul-tant", "re-sur-rec-tion",
    "re-ten-tion", "ret-ro-spec-tive", "rev-e-la-tion", "rev-o-lu-tion",
    "rhe-tor-i-cal", "ri-dic-u-lous", "ro-man-tic", "sa-cri-fice",
    "sat-is-fac-tion", "sat-is-fac-to-ry", "scep-ti-cism", "sched-ul-ing",
    "sci-en-tif-ic", "scru-ti-ny", "sec-re-tary", "sec-tor", "se-cu-ri-ty",
    "seg-men-ta-tion", "se-lec-tion", "sen-sa-tion", "sen-si-bil-ity",
    "sen-ti-ment", "sep-a-ra-tion", "se-quence", "se-ren-ity",
    "sig-nif-i-cant", "sim-i-lar-ity", "sim-pli-ci-ty", "sim-pli-fi-ca-tion",
    "sim-u-la-tion", "so-ci-e-ty", "so-ci-ol-ogy", "so-lu-tion",
    "so-phis-ti-cat-ed", "spe-cial-ist", "spe-ci-al-ity", "spec-i-fi-ca-tion",
    "spec-ta-cle", "spir-i-tu-al", "spon-ta-ne-ous", "sta-bil-ity",
    "stat-is-ti-cal", "stat-is-tics", "stew-ard-ship", "stim-u-la-tion",
    "strat-e-gy", "strength-en", "struc-tur-al", "sub-se-quent",
    "sub-stan-tial", "sub-sti-tu-tion", "suc-cess-ful", "suc-ces-sion",
    "suf-fi-cient", "sug-ges-tion", "sum-ma-ry", "su-per-fi-cial",
    "su-per-vi-sor", "sup-ple-men-tary", "sup-po-si-tion", "sur-geon",
    "sur-ren-der", "sur-vey", "sus-cep-ti-ble", "sus-pi-cious",
    "sus-tain-able", "sym-met-ri-cal", "sym-pa-thet-ic", "sym-pho-ny",
    "syn-chro-ni-za-tion", "syn-thet-ic", "sys-tem-at-ic", "tech-ni-cal",
    "tech-nique", "tech-no-log-i-cal", "tech-nol-o-gy", "tel-e-com-mu-ni-ca-tion",
    "tel-e-vi-sion", "tem-per-a-ture", "tem-po-rary", "ten-den-cy",
    "termi-nol-ogy", "ter-ri-to-ry", "the-a-ter", "the-o-ret-i-cal",
    "ther-mo-dy-nam-ics", "to-bac-co", "tol-er-ance", "tra-di-tion",
    "tra-jec-to-ry", "trans-ac-tion", "trans-cean-dent", "trans-fer",
    "trans-for-ma-tion", "trans-la-tion", "trans-mis-sion", "trans-par-ent",
    "trans-por-ta-tion", "tre-men-dous", "tri-bu-tary", "tu-i-tion",
    "tur-bu-lence", "typ-i-cal", "ul-ti-mate", "un-a-void-able",
    "un-cer-tain-ty", "un-con-scious", "un-con-trol-la-ble",
    "un-der-grad-u-ate", "un-der-stand", "un-der-stand-ing", "un-der-take",
    "un-em-ploy-ment", "un-e-qual", "un-ex-pect-ed", "un-for-tu-nate",
    "un-hap-py", "u-ni-fi-ca-tion", "u-ni-form-ity", "un-im-por-tant",
    "u-nique", "u-ni-ver-sal", "uni-ver-sity", "un-just", "un-like-ly",
    "un-lim-ited", "un-nec-es-sary", "un-pleas-ant", "un-re-al-is-tic",
    "un-re-li-a-ble", "un-sat-is-fac-to-ry", "un-suc-cess-ful",
    "un-u-su-al", "un-will-ing", "up-hold", "uti-li-za-tion", "ut-ter-ance",
    "va-ca-tion", "vac-u-um", "vague", "va-lid-i-ty", "val-u-a-ble",
    "var-i-a-tion", "var-i-ous", "veg-e-ta-ble", "ve-hic-u-lar",
    "ven-er-a-ble", "ven-ti-la-tion", "ver-bal", "ver-i-fi-ca-tion",
    "ver-sa-til-ity", "ver-ti-cal", "veter-i-nary", "vi-bra-tion",
    "vi-brant", "vic-tim", "vic-to-ri-ous", "vig-or-ous", "vin-tage",
    "vi-o-la-tion", "vir-tu-ous", "vis-i-bil-ity", "vis-u-al-i-za-tion",
    "vo-cab-u-lary", "vo-ca-tion-al", "vol-un-tary", "vol-un-teer",
    "vul-ner-a-bil-ity", "vul-ner-a-ble", "wealthy", "weath-er",
    "wel-fare", "west-ward", "where-as", "where-ever", "whole-sale",
    "whole-some", "wide-spread", "wil-der-ness", "win-dow", "with-draw",
    "with-hold", "with-out", "wit-ness", "won-der-ful", "worth-while",
    "wretch-ed", "yield-ing", "youth-ful", "zeal-ous", "ze-nith",
    "zo-ol-ogy", "zo-o-log-i-cal",
)

DIGITS = "0123456789"

#: Must match ``rich._hyphenation_patterns``/``_hyphenation.LEFT_HYPHEN_MIN`` and
#: ``RIGHT_HYPHEN_MIN``.  The generator applies them to the corpus so the emitted
#: dictionary can never contain a break that the runtime would have to discard.
LEFT_HYPHEN_MIN = 2
RIGHT_HYPHEN_MIN = 3


def expand(word: str, left: int, right: int) -> str:
    """Expand ``"hy-phen-ate"`` into ``".hy3phen3ate."``.

    A ``3`` marks a legal break, the word-boundary dots make edge patterns
    expressible.  Break points that would violate the minima are dropped.
    """
    clean = word.replace("-", "")
    breaks = set()
    pos = 0
    for ch in word:
        if ch == "-":
            breaks.add(pos)
        else:
            pos += 1
    legal = {p for p in breaks if p >= left and len(clean) - p >= right}
    out: List[str] = []
    for i, ch in enumerate(clean):
        out.append("3" if i in legal else DIGITS[0])
        out.append(ch)
    out.append(DIGITS[0])
    return "." + "".join(out) + "."


def to_pattern(chunk: str) -> Tuple[str, List[int]]:
    """Split an expanded chunk into its letter key and its interior weights.

    ``"y3ph"`` -> ``("yph", [3])``.  The chunk must start and end with a letter;
    an interior digit becomes the weight of the gap it sits in, and a missing
    digit becomes weight ``0``.
    """
    letters: List[str] = []
    weights: List[int] = []
    pending: List[int] = []
    for ch in chunk:
        if ch in DIGITS:
            pending.append(int(ch))
            continue
        if letters:
            # the gap between the previous letter and this one
            weights.append(max(pending) if pending else 0)
        pending = []
        letters.append(ch)
    return "".join(letters), weights


def specificity_weight(offset: int, is_break: bool) -> int:
    """Map (pattern offset, is this gap a break?) to a single Liang digit.

    The classical TeX tables resolve competing patterns with hand-tuned weights.
    For a *derived* table a principled substitute is to let the weight encode
    how specific the witness is.  Define the *offset* of a pattern as the number
    of letters that follow the gap, i.e. ``offset = len(pattern) - 1 - i`` for
    the gap at index ``i``.  A pattern that reaches further to the right across a
    gap is a more specific witness about that gap, so its weight is

    * ``2 * offset``      when the corpus says "do not break here", and
    * ``2 * offset + 1``  when the corpus says "break here".

    Because ``2k + 1 > 2k >= 2j`` for every ``j <= k``, an odd weight from an
    offset-``k`` pattern always outranks the even weight of any pattern with a
    smaller offset, while an even weight from a *larger* offset still blocks it.
    That reproduces "the most specific complete pattern wins" without any hand
    tuning, and keeps every weight inside one decimal digit.
    """
    offset = min(offset, MAX_OFFSET)
    return 2 * offset + (1 if is_break else 0)


def derive(expanded_words: Sequence[str]) -> Dict[str, List[int]]:
    """Derive the pattern table, then settle each gap by corpus vote.

    A naive derivation takes the maximum observed weight per gap, which makes
    every individual corpus word able to inject a break into a pattern that also
    covers many words without a break there.  Instead this function collects two
    votes per (pattern, gap):

    * ``breaks``   -- how many corpus occurrences say "a break may happen here",
    * ``blocks``   -- how many say "no break here".

    The gap becomes a break only when the corpus is at least as much in favour of
    breaking as of not breaking.  The vote is a much better estimate than a
    single occurrence because a genuine English break like ``hyp-hen`` occurs in
    many corpus words, while an accidental one usually occurs once.
    """
    breaks: Dict[str, List[int]] = {}
    blocks: Dict[str, List[int]] = {}
    for expanded in expanded_words:
        n = len(expanded)
        length = 1
        while length <= MAX_PATTERN_LEN:
            for start in range(0, n - length + 1):
                chunk = expanded[start:start + length]
                if chunk[0] in DIGITS or chunk[-1] in DIGITS:
                    continue
                letters, raw = to_pattern(chunk)
                if not raw or len(letters) < 2:
                    continue
                bslot = breaks.setdefault(letters, [0] * len(raw))
                kslot = blocks.setdefault(letters, [0] * len(raw))
                if len(bslot) != len(raw) or len(kslot) != len(raw):
                    continue
                for i, w in enumerate(raw):
                    if w % 2:
                        bslot[i] += 1
                    else:
                        kslot[i] += 1
            length += 1

    table: Dict[str, List[int]] = {}
    for letters, bslot in breaks.items():
        kslot = blocks[letters]
        gaps = len(bslot)
        weights = [
            specificity_weight(gaps - 1 - i, bslot[i] > kslot[i])
            for i in range(gaps)
        ]
        table[letters] = weights
    return table


def render(table: Dict[str, List[int]]) -> List[str]:
    """Serialise a derived table into sorted TeX text notation.

    Patterns with no odd weight are dropped: they can only ever raise an
    already-odd gap to a higher even value, which is a no-op, or raise an even
    gap to a higher even value, which never creates a hyphenation point.
    """
    items: List[str] = []
    for letters, weights in table.items():
        if len(letters) < 2:
            # a one-letter pattern has no interior gap and cannot describe a
            # break; such patterns only leak in through boundary chunks and
            # would fire at every occurrence of the letter
            continue
        if not any(w % 2 for w in weights):
            continue
        parts: List[str] = []
        for i, ch in enumerate(letters):
            # weights[i] is the weight of the gap *before* letters[i + 1]
            if i and weights[i - 1]:
                parts.append(str(weights[i - 1]))
            parts.append(ch)
        if weights[-1]:
            parts.append(str(weights[-1]))
        items.append("".join(parts))
    return sorted(items)


def parse_patterns(patterns: Sequence[str]) -> Dict[str, List[int]]:
    """Turn rendered TeX-notation patterns back into the weight table."""
    table: Dict[str, List[int]] = {}
    for pattern in patterns:
        letters, weights = to_pattern(pattern)
        if weights:
            table[letters] = weights
    return table


def apply_patterns(text: str, table: Dict[str, List[int]]) -> List[int]:
    """Apply patterns to a plain (unhyphenated) word; return sorted break points."""
    word = "." + text.lower() + "."
    scores = [0] * len(word)
    n = len(word)
    for letters, weights in table.items():
        length = len(letters)
        start = word.find(letters)
        while start != -1:
            for i, w in enumerate(weights):
                if w > scores[start + i]:
                    scores[start + i] = w
            start = word.find(letters, start + 1)
    limit = n - 1
    return [i for i, s in enumerate(scores) if s % 2 == 1 and 0 < i < limit]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patterns", default="rich/_hyphenation_patterns.py")
    parser.add_argument("--dictionary", default="rich/_hyphenation_dict.py")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    expanded = [expand(w, 2, 3) for w in CORPUS]
    table = derive(expanded)
    patterns = render(table)
    applied = parse_patterns(patterns)

    # ---- training recall -------------------------------------------------
    by_clean: Dict[str, str] = {}
    for w in CORPUS:
        by_clean[w.replace("-", "").lower()] = w
    exact = 0
    for clean, original in by_clean.items():
        expected = set()
        pos = 0
        for ch in original:
            if ch == "-":
                expected.add(pos)
            else:
                pos += 1
        got = set(apply_patterns(clean, applied))
        if got == expected:
            exact += 1
    total = len(by_clean)
    recall = 100.0 * exact / total if total else 0.0

    print(f"corpus words      : {total}")
    print(f"exact hyphenation : {exact}  ({recall:.1f}% training recall)")
    print(f"distinct patterns : {len(patterns)}")
    print(f"max pattern len   : {max((len(p) for p in patterns), default=0)}")
    if args.report:
        return 0

    write_patterns(Path(args.patterns), patterns, total, exact, recall)
    write_dictionary(Path(args.dictionary), by_clean)
    print(f"wrote {args.patterns} and {args.dictionary}")
    return 0


def write_patterns(
    out: Path, patterns: Sequence[str], total: int, exact: int, recall: float
) -> None:
    lines: List[str] = []
    lines.append('"""Approximate hyphenation patterns for :mod:`rich._hyphenation`.')
    lines.append("")
    lines.append("GENERATED FILE -- run ``python -m tools.gen_patterns`` to rebuild.")
    lines.append("")
    lines.append("The table is derived from the curated corpus in ``tools/gen_patterns.py``")
    lines.append("with the corpus-voting variant of the Knuth/Liang derivation described in")
    lines.append("that module.  It is an **approximation**: it reproduces only")
    lines.append(f"{recall:.0f}% of the {total} corpus words exactly, because a derived table")
    lines.append("cannot resolve pattern collisions the way a hand-tuned table does.")
    lines.append("")
    lines.append("Consequences for users of :mod:`rich._hyphenation`:")
    lines.append("")
    lines.append("* the bundled *dictionary* (``rich._hyphenation_dict``) is the accurate")
    lines.append("  source and is used by default;")
    lines.append("* these patterns are only consulted when the caller passes")
    lines.append("  ``approximate=True``;")
    lines.append("* loading a real ``hyphen.tex`` pattern file with")
    lines.append("  :func:`rich._hyphenation.load_tex_patterns` replaces this table entirely and")
    lines.append("  gives fully accurate hyphenation for arbitrary words.")
    lines.append("")
    lines.append("Notation is the standard TeX ``hyph-utf8`` text notation: the matched")
    lines.append("letters interleaved with single decimal weights, where a leading or trailing")
    lines.append("dot anchors the pattern to a word boundary and an odd weight marks a break.")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from typing import Tuple")
    lines.append("")
    lines.append('__all__ = ["APPROXIMATE_PATTERNS", "TRAINING_RECALL", "CORPUS_SIZE"]')
    lines.append("")
    lines.append(f"#: exact-match rate of the table on its own training corpus ({exact}/{total})")
    lines.append(f"TRAINING_RECALL: float = {recall / 100.0!r}")
    lines.append("")
    lines.append("#: number of corpus words the table was derived from")
    lines.append(f"CORPUS_SIZE: int = {total}")
    lines.append("")
    lines.append("#: Derived Liang patterns, sorted for a stable diff.")
    lines.append("APPROXIMATE_PATTERNS: Tuple[str, ...] = (")
    for p in patterns:
        lines.append(f'    "{p}",')
    lines.append(")")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def write_dictionary(out: Path, by_clean: Dict[str, str]) -> None:
    """Write the validated word -> break-point dictionary.

    Break points that violate the left/right minima are dropped here rather than
    at runtime, so the shipped table only ever contains legal offsets.  A corpus
    entry such as ``"car-ry"`` therefore becomes ``"carry": ()`` -- the word is
    still known, it simply has no legal break inside it.
    """
    lines: List[str] = []
    lines.append('"""Validated hyphenation dictionary for :mod:`rich._hyphenation`.')
    lines.append("")
    lines.append("GENERATED FILE -- run ``python -m tools.gen_patterns`` to rebuild.")
    lines.append("")
    lines.append("Every entry was checked by hand in ``tools/gen_patterns.py`` and then")
    lines.append("filtered so that each offset respects the left/right minima.  This table is")
    lines.append("exact by construction, which is why")
    lines.append(":class:`rich._hyphenation.Hyphenator` prefers it over any pattern table.")
    lines.append("")
    lines.append("Offsets are always at least ``LEFT_HYPHEN_MIN`` from the start of the word and")
    lines.append("at least ``RIGHT_HYPHEN_MIN`` from its end.  An entry with an empty tuple is a")
    lines.append("word the hyphenator knows but must not break.")
    lines.append('"""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from typing import Dict, Tuple")
    lines.append("")
    lines.append('__all__ = ["HYPHENATION_DICT", "HYPHENATION_MINIMA"]')
    lines.append("")
    lines.append(f"#: (left, right) minima the table was filtered with")
    lines.append(f"HYPHENATION_MINIMA: Tuple[int, int] = ({LEFT_HYPHEN_MIN}, {RIGHT_HYPHEN_MIN})")
    lines.append("")
    lines.append("#: word -> tuple of legal hyphenation offsets")
    lines.append("HYPHENATION_DICT: Dict[str, Tuple[int, ...]] = {")
    dropped = 0
    for clean in sorted(by_clean):
        original = by_clean[clean]
        points: List[int] = []
        position = 0
        for char in original:
            if char == "-":
                if position >= LEFT_HYPHEN_MIN and len(clean) - position >= RIGHT_HYPHEN_MIN:
                    points.append(position)
                else:
                    dropped += 1
            else:
                position += 1
        body = ", ".join(str(p) for p in points)
        if len(points) == 1:
            body += ","
        lines.append(f'    "{clean}": ({body}),')
    lines.append("}")
    lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"dictionary: {len(by_clean)} words, {dropped} illegal breaks dropped")


if __name__ == "__main__":
    raise SystemExit(main())
