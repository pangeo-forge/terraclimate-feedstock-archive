#!/usr/bin/env python3


def main():
    """Run this repositories pipeline

    Currently just prints the flow property.
    """
    from recipe.pipeline import pipeline

    pipeline.flow.validate()

    print(pipeline.flow)
    print(pipeline.flow.environment)
    print(pipeline.flow.parameters)
    print(pipeline.flow.sorted_tasks())

    # pipeline.flow.run()


if __name__ == "__main__":
    main()
