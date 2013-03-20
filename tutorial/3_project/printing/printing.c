#include "printing.h"
#include "math/math.h"

#include <stdio.h>

void print_sum(int a, int b)
{
    printf("%d + %d = %d\n", a, b, sum(a, b));
    printf("The defined value in printing.c is %d\n", DEFINED_VALUE);
}
