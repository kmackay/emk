#include "library.h"
#include "revision.h"

#include <stdio.h>

void print_revision_info(void)
{
    printf("Revision %s from %s\n", REVISION, URL);
}
