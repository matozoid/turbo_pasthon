#include <stdio.h>
typedef struct { long a, b, c; } Big;
typedef struct { double x, y; } Pt;
typedef struct { int n; float f; } Mix;
long sumBig(Big b) { return b.a * 100 + b.b * 10 + b.c; }
Big makeBig(long a, long b, long c) { Big r = {a, b, c}; return r; }
double late(long a, long b, long c, long d, long e, Pt p, long f, Pt q, Mix m, double r1, double r2, double r3, double r4, double r5, double r6, double r7) {
    printf("C: %ld %ld %ld %ld %ld (%g,%g) %ld (%g,%g) {%d,%g} %g %g %g %g %g %g %g\n", a,b,c,d,e,p.x,p.y,f,q.x,q.y,m.n,m.f,r1,r2,r3,r4,r5,r6,r7);
    fflush(stdout);
    return p.x + q.y + r7;
}
double callBack(double (*f)(Pt, Big, Mix, long, long, long, long, long, long, long, double)) {
    Pt p = {0.5, 1.5}; Big b = {1, 2, 3}; Mix m = {7, 0.25f};
    return f(p, b, m, 1, 2, 3, 4, 5, 6, 7, 8.5);
}
long callLate(long (*f)(long, long, long, long, long, long, Mix, float)) {
    Mix m = {9, 0.5f};
    return f(1, 2, 3, 4, 5, 6, m, 1.25f);
}
